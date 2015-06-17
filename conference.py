#!/usr/bin/env python

"""
conference.py -- Udacity conference server-side Python App Engine API;
    uses Google Cloud Endpoints

$Id: conference.py,v 1.25 2014/05/24 23:42:19 wesc Exp wesc $

created by wesc on 2014 apr 21

"""

__author__ = 'wesc+api@google.com (Wesley Chun)'

from google.appengine.api import memcache

from datetime import datetime

import endpoints
from protorpc import messages
from protorpc import message_types
from protorpc import remote

from google.appengine.api import memcache
from google.appengine.api import taskqueue
from google.appengine.ext import ndb

from models import Profile
from models import ProfileMiniForm
from models import ProfileForm
from models import TeeShirtSize
from models import Conference
from models import ConferenceForm
from models import ConferenceForms
from models import ConferenceQueryForm
from models import ConferenceQueryForms
from models import BooleanMessage
from models import StringMessage
from models import ConflictException

from models import Session
from models import SessionForm
from models import SessionForms

from utils import getUserId
import json

from settings import WEB_CLIENT_ID

EMAIL_SCOPE = endpoints.EMAIL_SCOPE
API_EXPLORER_CLIENT_ID = endpoints.API_EXPLORER_CLIENT_ID
MEMCACHE_ANNOUNCEMENTS_KEY = "RECENT_ANNOUNCEMENTS"

# - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -

DEFAULTS = {
    "city": "Default City",
    "maxAttendees": 0,
    "seatsAvailable": 0,
    "topics": [ "Default", "Topic" ],
}

OPERATORS = {
            'EQ':   '=',
            'GT':   '>',
            'GTEQ': '>=',
            'LT':   '<',
            'LTEQ': '<=',
            'NE':   '!='
            }

FIELDS =    {
            'CITY': 'city',
            'TOPIC': 'topics',
            'MONTH': 'month',
            'MAX_ATTENDEES': 'maxAttendees',
            }


CONF_GET_REQUEST = endpoints.ResourceContainer(
    message_types.VoidMessage,
    websafeConferenceKey=messages.StringField(1),
)

CONF_POST_REQUEST = endpoints.ResourceContainer(
    ConferenceForm,
    websafeConferenceKey=messages.StringField(1),
)

SESS_SPKR_GET_REQUEST = endpoints.ResourceContainer(
    message_types.VoidMessage,
    speaker=messages.StringField(1),
)

SESS_CONF_SPKR_GET_REQUEST = endpoints.ResourceContainer(
    message_types.VoidMessage,
    websafeConferenceKey=messages.StringField(1),
    speaker=messages.StringField(2),
)

SESS_TYPE_GET_REQUEST = endpoints.ResourceContainer(
    message_types.VoidMessage,
    type=messages.StringField(1),
)

SESS_CONF_TYPE_GET_REQUEST = endpoints.ResourceContainer(
    message_types.VoidMessage,
    websafeConferenceKey=messages.StringField(1),
    type=messages.StringField(2),
)

SESS_CONF_GET_REQUEST = endpoints.ResourceContainer(
    message_types.VoidMessage,
    websafeConferenceKey=messages.StringField(1),
)

SESS_WISH = endpoints.ResourceContainer(
    message_types.VoidMessage,
    websafeSessionKey=messages.StringField(1),
)

SESS_POST_REQUEST = endpoints.ResourceContainer(
    SessionForm,
    websafeConferenceKey=messages.StringField(1),
)
class SESS_CONF_RESPONSE(messages.Message):
    email = messages.MessageField(SessionForm, 1)


class Object:
    def to_JSON(self):
        return json.dumps(self, default=lambda o: o.__dict__, 
            sort_keys=True, indent=4)

# - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -


@endpoints.api( name='conference',
                version='v1',
                allowed_client_ids=[WEB_CLIENT_ID, API_EXPLORER_CLIENT_ID],
                scopes=[EMAIL_SCOPE])
class ConferenceApi(remote.Service):
    """Conference API v0.1"""

# - - - Profile objects - - - - - - - - - - - - - - - - - - -

    def _copyProfileToForm(self, prof):
        """Copy relevant fields from Profile to ProfileForm."""
        # copy relevant fields from Profile to ProfileForm
        pf = ProfileForm()
        for field in pf.all_fields():
            if hasattr(prof, field.name):
                # convert t-shirt string to Enum; just copy others
                if field.name == 'teeShirtSize':
                    setattr(pf, field.name, getattr(TeeShirtSize, getattr(prof, field.name)))
                else:
                    setattr(pf, field.name, getattr(prof, field.name))
        pf.check_initialized()
        return pf


    def _getProfileFromUser(self):
        """Return user Profile from datastore, creating new one if non-existent."""
        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('Authorization required')

        # get Profile from datastore
        user_id = getUserId(user)
        p_key = ndb.Key(Profile, user_id)
        profile = p_key.get()
        # create new Profile if not there
        if not profile:
            profile = Profile(
                key = p_key,
                displayName = user.nickname(),
                mainEmail= user.email(),
                teeShirtSize = str(TeeShirtSize.NOT_SPECIFIED),
            )
            profile.put()

        return profile      # return Profile


    def _doProfile(self, save_request=None):
        """Get user Profile and return to user, possibly updating it first."""
        # get user Profile
        prof = self._getProfileFromUser()

        # if saveProfile(), process user-modifyable fields
        if save_request:
            for field in ('displayName', 'teeShirtSize'):
                if hasattr(save_request, field):
                    val = getattr(save_request, field)
                    if val:
                        setattr(prof, field, str(val))
                        prof.put()

        # return ProfileForm
        return self._copyProfileToForm(prof)


    @endpoints.method(message_types.VoidMessage, ProfileForm,
            path='profile', http_method='GET', name='getProfile')
    def getProfile(self, request):
        """Return user profile."""
        return self._doProfile()


    @endpoints.method(ProfileMiniForm, ProfileForm,
            path='profile', http_method='POST', name='saveProfile')
    def saveProfile(self, request):
        """Update & return user profile."""
        return self._doProfile(request)


# - - - Conference objects - - - - - - - - - - - - - - - - - - -

    def _copyConferenceToForm(self, conf, displayName):
        """Copy relevant fields from Conference to ConferenceForm."""
        cf = ConferenceForm()
        for field in cf.all_fields():
            if hasattr(conf, field.name):
                # convert Date to date string; just copy others
                if field.name.endswith('Date'):
                    setattr(cf, field.name, str(getattr(conf, field.name)))
                else:
                    setattr(cf, field.name, getattr(conf, field.name))
            elif field.name == "websafeKey":
                setattr(cf, field.name, conf.key.urlsafe())
        if displayName:
            setattr(cf, 'organizerDisplayName', displayName)
        cf.check_initialized()
        return cf


    def _createConferenceObject(self, request):
        """Create or update Conference object, returning ConferenceForm/request."""
        # preload necessary data items
        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('Authorization required')
        user_id = getUserId(user)

        if not request.name:
            raise endpoints.BadRequestException("Conference 'name' field required")

        # copy ConferenceForm/ProtoRPC Message into dict
        data = {field.name: getattr(request, field.name) for field in request.all_fields()}
        del data['websafeKey']
        del data['organizerDisplayName']

        # add default values for those missing (both data model & outbound Message)
        for df in DEFAULTS:
            if data[df] in (None, []):
                data[df] = DEFAULTS[df]
                setattr(request, df, DEFAULTS[df])

        # convert dates from strings to Date objects; set month based on start_date
        if data['startDate']:
            data['startDate'] = datetime.strptime(data['startDate'][:10], "%Y-%m-%d").date()
            data['month'] = data['startDate'].month
        else:
            data['month'] = 0
        if data['endDate']:
            data['endDate'] = datetime.strptime(data['endDate'][:10], "%Y-%m-%d").date()

        # set seatsAvailable to be same as maxAttendees on creation
        # both for data model & outbound Message
        if data["maxAttendees"] > 0:
            data["seatsAvailable"] = data["maxAttendees"]
            setattr(request, "seatsAvailable", data["maxAttendees"])

        # make Profile Key from user ID
        p_key = ndb.Key(Profile, user_id)
        # allocate new Conference ID with Profile key as parent
        c_id = Conference.allocate_ids(size=1, parent=p_key)[0]
        # make Conference key from ID
        c_key = ndb.Key(Conference, c_id, parent=p_key)
        data['key'] = c_key
        data['organizerUserId'] = request.organizerUserId = user_id

        # create Conference & return (modified) ConferenceForm
        Conference(**data).put()
        taskqueue.add(params={'email': user.email(),
            'conferenceInfo': repr(request)},
            url='/tasks/send_confirmation_email'
        )

        return request

    def _createSessionObject(self, request):
        """Create or update Session object, returning SessionForm/request.  open only to the organizer of the conference"""
        """Ideally, create the session as a child of the conference. """
        print ("In _createSessionObject")
        # preload necessary data items
        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('Authorization required')
        user_id = getUserId(user)
        print("user_id: {}", user_id)
        print("user: {}", repr(user))

        # copy ConferenceForm/ProtoRPC Message into dict
        data = {field.name: getattr(request, field.name) for field in request.all_fields()}
        print("data: {}", repr(data))

        del data['websafeKey']

        conf_name = data['conferenceName']
        print("conf_name: {}", conf_name)

        q = Conference.query()
        q= q.filter(Conference.name == conf_name)

        conf = q.get()
        if not conf:
            raise endpoints.UnauthorizedException('Session must belong to a conference.')

        print("conf: {}", repr(conf))

        if user_id != conf.organizerUserId:
            raise endpoints.BadRequestException("Session parent must be the same as the current user.")

        if not request.name:
            raise endpoints.BadRequestException("Session 'name' field required")


        # convert dates from strings to Date objects; 
        if data['startDate']:
            data['startdatetime'] = datetime.strptime(data['startDate'][:10], "%Y-%m-%d").date()
        if data['startTime']:
            data['startTime'] = datetime.strptime(data['startTime'][:10], "%h:%m:%s").time()
            data['startdatetime'] = datetime.datetime.combine(data['startdatetime'], data['startTime'])
        del data['startDate']
        del data['startTime']


        # allocate new Session ID with Conference key as parent
        s_id = Session.allocate_ids(size=1, parent=conf.key)[0]
        # make Session key from ID
        s_key = ndb.Key(Session, s_id, parent=conf.key)
        data['key'] = s_key
        # data['organizerUserId'] = request.organizerUserId = user_id

        # create Session & return (modified) SessionForm
        Session(**data).put()
        # taskqueue.add(params={'email': user.email(),
        #     'conferenceInfo': repr(request)},
        #     url='/tasks/send_confirmation_email'
        # )

        return request

# - - - Session objects - - - - - - - - - - - - - - - - - - -

    def _copySessionToForm(self, sess):
        """Copy relevant fields from Session to SessionForm."""
        sf = SessionForm()
        for field in sf.all_fields():
            if hasattr(sess, field.name):
                # convert Date to date string; just copy others
                if field.name.endswith('Date'):
                    setattr(sf, field.name, str(getattr(sess, field.name)))
                else:
                    setattr(sf, field.name, getattr(sess, field.name))
            elif field.name == "websafeKey":
                setattr(sf, field.name, sess.key.urlsafe())
        sf.check_initialized()
        return sf

    def _getQuery(self, request):
        """Return formatted query from the submitted filters."""
        q = Conference.query()
        inequality_filter, filters = self._formatFilters(request.filters)

        # If exists, sort on inequality filter first
        if not inequality_filter:
            q = q.order(Conference.name)
        else:
            q = q.order(ndb.GenericProperty(inequality_filter))
            q = q.order(Conference.name)

        for filtr in filters:
            if filtr["field"] in ["month", "maxAttendees"]:
                filtr["value"] = int(filtr["value"])
            formatted_query = ndb.query.FilterNode(filtr["field"], filtr["operator"], filtr["value"])
            q = q.filter(formatted_query)
        return q


    def _formatFilters(self, filters):
        """Parse, check validity and format user supplied filters."""
        formatted_filters = []
        inequality_field = None

        for f in filters:
            filtr = {field.name: getattr(f, field.name) for field in f.all_fields()}

            try:
                filtr["field"] = FIELDS[filtr["field"]]
                filtr["operator"] = OPERATORS[filtr["operator"]]
            except KeyError:
                raise endpoints.BadRequestException("Filter contains invalid field or operator.")

            # Every operation except "=" is an inequality
            if filtr["operator"] != "=":
                # check if inequality operation has been used in previous filters
                # disallow the filter if inequality was performed on a different field before
                # track the field on which the inequality operation is performed
                if inequality_field and inequality_field != filtr["field"]:
                    raise endpoints.BadRequestException("Inequality filter is allowed on only one field.")
                else:
                    inequality_field = filtr["field"]

            formatted_filters.append(filtr)
        return (inequality_field, formatted_filters)

    @ndb.transactional()
    def _updateConferenceObject(self, request):
        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('Authorization required')
        user_id = getUserId(user)

        # copy ConferenceForm/ProtoRPC Message into dict
        data = {field.name: getattr(request, field.name) for field in request.all_fields()}

        # update existing conference
        conf = ndb.Key(urlsafe=request.websafeConferenceKey).get()
        # check that conference exists
        if not conf:
            raise endpoints.NotFoundException(
                'No conference found with key: %s' % request.websafeConferenceKey)

        # check that user is owner
        if user_id != conf.organizerUserId:
            raise endpoints.ForbiddenException(
                'Only the owner can update the conference.')

        # Not getting all the fields, so don't create a new object; just
        # copy relevant fields from ConferenceForm to Conference object
        for field in request.all_fields():
            data = getattr(request, field.name)
            # only copy fields where we get data
            if data not in (None, []):
                # special handling for dates (convert string to Date)
                if field.name in ('startDate', 'endDate'):
                    data = datetime.strptime(data, "%Y-%m-%d").date()
                    if field.name == 'startDate':
                        conf.month = data.month
                # write to Conference object
                setattr(conf, field.name, data)
        conf.put()
        prof = ndb.Key(Profile, user_id).get()
        return self._copyConferenceToForm(conf, getattr(prof, 'displayName'))


    @endpoints.method(ConferenceForm, ConferenceForm, path='conference',
            http_method='POST', name='createConference')
    def createConference(self, request):
        """Create new conference."""
        return self._createConferenceObject(request)

    @endpoints.method(CONF_POST_REQUEST, ConferenceForm,
            path='conference/{websafeConferenceKey}',
            http_method='PUT', name='updateConference')
    def updateConference(self, request):
        """Update conference w/provided fields & return w/updated info."""
        return self._updateConferenceObject(request)

    @endpoints.method(ConferenceQueryForms, ConferenceForms,
            path='queryConferences', http_method='POST', name='queryConferences')
    def queryConferences(self, request):
        """Query for conferences."""
        conferences = self._getQuery(request)

         # return individual ConferenceForm object per Conference
        return ConferenceForms(
            items=[self._copyConferenceToForm(conf, "") \
            for conf in conferences]
        )
        
    @endpoints.method(message_types.VoidMessage, ConferenceForms,
            path='getConferencesCreated',
            http_method='POST', name='getConferencesCreated')
    def getConferencesCreated(self, request):
        """Return conferences created by user."""
        # make sure user is authed
        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('Authorization required')

        # make profile key
        p_key = ndb.Key(Profile, getUserId(user))
        # create ancestor query for this user
        conferences = Conference.query(ancestor=p_key)
        # get the user profile and display name
        prof = p_key.get()
        displayName = getattr(prof, 'displayName')
        # return set of ConferenceForm objects per Conference
        return ConferenceForms(
            items=[self._copyConferenceToForm(conf, displayName) for conf in conferences]
        )

    @endpoints.method(message_types.VoidMessage, ConferenceForms,
            path='filterPlayground',
            http_method='GET', name='filterPlayground')
    def filterPlayground(self, request):
        q = Conference.query()
        # simple filter usage:
        # q = q.filter(Conference.city == "Paris")

        # advanced filter building and usage
        field = "city"
        operator = "="
        value = "London"
        f = ndb.query.FilterNode(field, operator, value)
        q = q.filter(f)

        # TODO
        # add 2 filters:
        # 1: city equals to London
        # 2: topic equals "Medical Innovations"
        q= q.filter(Conference.topics == "Web Technologies")
        q = q.order(Conference.name)
        q= q.filter(Conference.maxAttendees > 6)

        return ConferenceForms(
            items=[self._copyConferenceToForm(conf, "") for conf in q]
        )


# - - - Sessions - - - - - - - - - - - - - - - - - - - -
    # @endpoints.method(CONF_GET_REQUEST, ConferenceForm,
    #         path='conference/{websafeConferenceKey}',
    #         http_method='GET', name='getConference')
    # def getConference(self, request):
    #     """Return requested conference (by websafeConferenceKey)."""
    #     # get Conference object from request; bail if not found
    #     conf = ndb.Key(urlsafe=request.websafeConferenceKey).get()
    #     if not conf:                                                                                                                                                               raise endpoints.NotFoundException(
    #             'No conference found with key: %s' % request.websafeConferenceKey)
    #     prof = conf.key.parent().get()

    @endpoints.method(SESS_CONF_GET_REQUEST, SessionForms, path='session/{websafeConferenceKey}',
            http_method='GET', name='getConferenceSessions')
    def getConferenceSessions(self, request):
        """ Given a conference, return all sessions """
        print("in getConferenceSessions")
        print("request: {}", repr(request))
        print("request.websafeConferenceKey: {}", repr(request.websafeConferenceKey))
        # conf_name = request.websafeConferenceKey
        # print("conf_name: {}", conf_name)
        # q = Conference.query()
        # q= q.filter(Conference.name == request.websafeConferenceKey)
        # conf = q.get()

        conf_key = ndb.Key(urlsafe= request.websafeConferenceKey)
        conf = conf_key.get()
        print("conf: {}", repr(conf))
        if not conf:                                                                                                                                                               raise endpoints.NotFoundException(
            'No conference found with key: %s' % request.websafeConferenceKey)
        sessions = Session.query(ancestor=conf.key)
        # sessions = Session.query(ancestor=ndb.Key(urlsafe=request.websafeConferenceKey))
        session = sessions.get()
        print("session: {}", session)
        # response = SESS_CONF_RESPONSE()
        # # print("session dict: {}", session.to_JSON())
        # response.email = self._copySessionToForm(session)
        # print("response.email: {}", response.email)
        # return response
        # return self._copySessionsToForm(sessions, getattr(prof, 'displayName'))

        #  fetch sessions from datastore. 
        # sessions = ndb.Key(urlsafe=request.websafeConferenceKey).get())

        # return set of ConferenceForm objects per Conference
        return SessionForms(items=[self._copySessionToForm(sess)\
            for sess in sessions]
        )


    @endpoints.method(SESS_TYPE_GET_REQUEST, SessionForms, path='session/bytype',
            http_method='GET', name='getSessionsByType')
    def getSessionsByType(self, request):
        """ Given a session type, return all sessions given of this type, across all conferences """
        sessions = Session.query(Session.typeOfSession == request.type)
        return SessionForms(items=[self._copySessionToForm(sess)\
            for sess in sessions]
        )

    @endpoints.method(SESS_CONF_TYPE_GET_REQUEST, SessionForms, path='session/bytypeinconference',
            http_method='GET', name='getConferenceSessionsByType')
    def getConferenceSessionsByType(self, request):
        """ Given a conference, return all sessions of a specified type (eg lecture, keynote, workshop) """
        # conf = ndb.Key(urlsafe=websafeConferenceKey).get()
        # if not conf:                                                                                                                                                               raise endpoints.NotFoundException(
        #         'No conference found with key: %s' % websafeConferenceKey)
        # sessions = Sessions.query(ancestor=conf.key).filter(Sessions.type == typeOfSession)
        # return sessions


        # q = Conference.query()
        # q= q.filter(Conference.name == request.websafeConferenceKey)
        # conf = q.get()

        conf_key = ndb.Key(urlsafe= request.websafeConferenceKey)
        conf = conf_key.get()
        if not conf:                                                                                                                                                               raise endpoints.NotFoundException(
                'No conference found with key: %s' % request.websafeConferenceKey)
        sessions = Session.query(ancestor=conf.key).filter(Session.typeOfSession == request.type)
        return SessionForms(items=[self._copySessionToForm(sess)\
            for sess in sessions]
        )

    @endpoints.method(SESS_SPKR_GET_REQUEST, SessionForms, path='session/byspeaker',
            http_method='GET', name='getSessionsBySpeaker')
    def getSessionsBySpeaker(self, request):
        """ Given a speaker, return all sessions given by this particular speaker, across all conferences """
        sessions = Session.query(Session.speaker == request.speaker)
        return SessionForms(items=[self._copySessionToForm(sess)\
            for sess in sessions]
        )

    @endpoints.method(SESS_CONF_SPKR_GET_REQUEST, SessionForms, path='session/byspeakerinconference',
            http_method='GET', name='getConferenceSessionsBySpeaker')
    def getConferenceSessionsBySpeaker(self, request):
        """ Given a speaker, return all sessions given by this particular speaker, across all conferences """
        conf_key = ndb.Key(urlsafe= request.websafeConferenceKey)
        conf = conf_key.get()
        if not conf:                                                                                                                                                               raise endpoints.NotFoundException(
                'No conference found with key: %s' % request.websafeConferenceKey)
        sessions = Session.query(ancestor=conf.key).filter(Session.speaker == request.speaker)
        return SessionForms(items=[self._copySessionToForm(sess)\
            for sess in sessions]
        )

    @endpoints.method(SessionForm, SessionForm, path='session',
            http_method='POST', name='createSession')
    def createSession(self, request):
        """ open only to the organizer of the conference """
        return self._createSessionObject(request)


# - - - Registration - - - - - - - - - - - - - - - - - - - -
    @ndb.transactional(xg=True)
    def _conferenceRegistration(self, request, reg=True):
        """Register or unregister user for selected conference."""
        retval = None
        prof = self._getProfileFromUser() # get user Profile

        # check if conf exists given websafeConfKey
        # get conference; check that it exists
        wsck = request.websafeConferenceKey
        conf = ndb.Key(urlsafe=wsck).get()
        if not conf:
            raise endpoints.NotFoundException(
                'No conference found with key: %s' % wsck)

        # register
        if reg:
            # check if user already registered otherwise add
            if wsck in prof.conferenceKeysToAttend:
                raise ConflictException(
                    "You have already registered for this conference")

            # check if seats avail
            if conf.seatsAvailable <= 0:
                raise ConflictException(
                    "There are no seats available.")

            # register user, take away one seat
            prof.conferenceKeysToAttend.append(wsck)
            conf.seatsAvailable = max(0, conf.seatsAvailable - 1)
            retval = True

        # unregister
        else:
            # check if user already registered
            if wsck in prof.conferenceKeysToAttend:

                # unregister user, add back one seat
                prof.conferenceKeysToAttend.remove(wsck)
                conf.seatsAvailable += 1
                retval = True
            else:
                retval = False

        # write things back to the datastore & return
        prof.put()
        conf.put()
        return BooleanMessage(data=retval)


    @endpoints.method(CONF_GET_REQUEST, BooleanMessage,
            path='conference/register/{websafeConferenceKey}',
            http_method='POST', name='registerForConference')
    def registerForConference(self, request):
        """Register user for selected conference."""
        return self._conferenceRegistration(request)

    @endpoints.method(CONF_GET_REQUEST, BooleanMessage,
            path='conference/unregister/{websafeConferenceKey}',
            http_method='POST', name='unregisterFromConference')
    def unregisterFromConference(self, request):
        """Unregister user for selected conference."""
        return self._conferenceRegistration(request, False)


    @endpoints.method(message_types.VoidMessage, ConferenceForms,
            path='conferences/attending',
            http_method='GET', name='getConferencesToAttend')
    def getConferencesToAttend(self, request):
        """Get list of conferences that user has registered for."""
        # TODO:
        # step 1: get user profile
        prof = self._getProfileFromUser() # get user Profile
        # step 2: get conferenceKeysToAttend from profile.
        # to make a ndb key from websafe key you can use:
        # ndb.Key(urlsafe=my_websafe_key_string)
        keys_to_attend = []
        for rawKey in prof.conferenceKeysToAttend:
            key = ndb.Key(urlsafe=rawKey)
            print("rawKey: {}", rawKey)
            keys_to_attend.append(key)
        # step 3: fetch conferences from datastore. 
        # for ndbKey in keys_to_attend:
        #     conf = ndbKey.get()
        # Use get_multi(array_of_keys) to fetch all keys at once.
        # Do not fetch them one by one!
        conferences = ndb.get_multi(keys_to_attend)

        # return set of ConferenceForm objects per Conference
        return ConferenceForms(items=[self._copyConferenceToForm(conf, "")\
            for conf in conferences]
        )

    @endpoints.method(CONF_GET_REQUEST, ConferenceForm,
            path='conference/{websafeConferenceKey}',
            http_method='GET', name='getConference')
    def getConference(self, request):
        """Return requested conference (by websafeConferenceKey)."""
        # get Conference object from request; bail if not found
        conf = ndb.Key(urlsafe=request.websafeConferenceKey).get()
        if not conf:                                                                                                                                                               raise endpoints.NotFoundException(
                'No conference found with key: %s' % request.websafeConferenceKey)
        prof = conf.key.parent().get()
        # return ConferenceForm
        return self._copyConferenceToForm(conf, getattr(prof, 'displayName'))

# - - - Session wishlists - - - - - - - - - - - - - - - - - -
    def _sessionWishlist(self, request, add=True):
        """Add or remove session to/from user's wishlist."""
        retval = None
        prof = self._getProfileFromUser() # get user Profile

        # check if conf exists given websafeConfKey
        # get conference; check that it exists
        wssk = request.websafeSessionKey
        sess = ndb.Key(urlsafe=wssk).get()
        if not sess:
            raise endpoints.NotFoundException(
                'No session found with key: %s' % wsck)

        # add
        if add:
            # check if user already wishing for this session otherwise add
            if wssk in prof.sessionKeysWishList:
                raise ConflictException(
                    "You are already interested in this session")

            # register user, take away one seat
            prof.sessionKeysWishList.append(wssk)
            retval = True

        # unregister
        else:
            # check if user already registered
            if wssk in prof.sessionKeysWishList:

                # unregister user, add back one seat
                prof.sessionKeysWishList.remove(wssk)
                retval = True
            else:
                retval = False

        # write things back to the datastore & return
        prof.put()
        return BooleanMessage(data=retval)

    @endpoints.method(SESS_WISH, BooleanMessage,
            path='session/addwish',
            http_method='POST', name='addSessionToWishlist')
    def addSessionToWishlist(self, request):
        """Add session to user's wishlist. It doesn't matter if the user is scheduled to attend the session's conference."""
        return self._sessionWishlist(request)

    @endpoints.method(SESS_WISH, BooleanMessage,
            path='session/removewish',
            http_method='POST', name='removeSessionFromWishlist')
    def removeSessionFromWishlist(self, request):
        """Remove session from user's wishlist."""
        return self._sessionWishlist(request, False)

    @endpoints.method(SESS_CONF_GET_REQUEST, SessionForms,
            path='sessions/wishlist',
            http_method='GET', name='getSessionsInWishlist')
    def getSessionsInWishlist(self, request):
        """Get list of sessions for a specific conference that user wishes to attend."""
        # TODO:
        # step 1: get user profile
        prof = self._getProfileFromUser() # get user Profile
        # step 2: get conferenceKeysToAttend from profile.
        # to make a ndb key from websafe key you can use:
        # ndb.Key(urlsafe=my_websafe_key_string)
        keys_to_attend = []
        for rawKey in prof.sessionKeysWishList:
            key = ndb.Key(urlsafe=rawKey)
            print("rawKey: {}", rawKey)
            keys_to_attend.append(key)
        # step 3: fetch conferences from datastore. 
        # for ndbKey in keys_to_attend:
        #     conf = ndbKey.get()
        # Use get_multi(array_of_keys) to fetch all keys at once.
        # Do not fetch them one by one!
        sessions = ndb.get_multi(keys_to_attend)
        conf_sessions = []
        conf_key = ndb.Key(Conference, request.websafeConferenceKey)
        for session in sessions:
            if session.key == conf_key:
                conf_sessions.add(session) 

        # return set of ConferenceForm objects per Conference
        return SessionForms(items=[self._copySessionToForm(sess, "")\
            for sess in conf_sessions]
        )


# - - - Announcements - - - - - - - - - - - - - - - - - - - -

    @staticmethod
    def _cacheAnnouncement():
        """Create Announcement & assign to memcache; used by
        memcache cron job & putAnnouncement().
        """
        confs = Conference.query(ndb.AND(
            Conference.seatsAvailable <= 5,
            Conference.seatsAvailable > 0)
        ).fetch(projection=[Conference.name])

        if confs:
            # If there are almost sold out conferences,
            # format announcement and set it in memcache
            announcement = '%s %s' % (
                'Last chance to attend! The following conferences '
                'are nearly sold out:',
                ', '.join(conf.name for conf in confs))
            memcache.set(MEMCACHE_ANNOUNCEMENTS_KEY, announcement)
        else:
            # If there are no sold out conferences,
            # delete the memcache announcements entry
            announcement = ""
            memcache.delete(MEMCACHE_ANNOUNCEMENTS_KEY)

        return announcement


    @endpoints.method(message_types.VoidMessage, StringMessage,
            path='conference/announcement/get',
            http_method='GET', name='getAnnouncement')
    def getAnnouncement(self, request):
        """Return Announcement from memcache."""
        # TODO 1
        # return an existing announcement from Memcache or an empty string.
        announcement = ""
        return StringMessage(data=announcement)



# registers API
api = endpoints.api_server([ConferenceApi]) 
