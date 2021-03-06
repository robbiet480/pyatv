"""Fake Apple TV device for tests.

This is an implementation of an Apple TV device that can be used to verify
functionality in tests. It is possible to specify return values for different
kinds of responses. Also, it performs various sanity checks, like that auth
information is correct and headers are present.
"""

import asyncio
from collections import namedtuple

from aiohttp import web

from pyatv import (tags, dmap, tag_definitions)


EXPECTED_HEADERS = {
    'Accept': '*/*',
    'Accept-Encoding': 'gzip',
    'Client-DAAP-Version': '3.12',
    'Client-ATV-Sharing-Version': '1.2',
    'Client-iTunes-Sharing-Version': '3.10',
    'User-Agent': 'TVRemote/186 CFNetwork/808.1.4 Darwin/16.1.0',
    'Viewer-Only-Client': '1',
}

LoginResponse = namedtuple('LoginResponse', 'session, status')


class PlayingResponse:
    """Response returned by command playstatusupdate."""

    def __init__(self, **kwargs):
        """Initialize a new PlayingResponse."""
        self.paused = self._get('paused', **kwargs)
        self.title = self._get('title', **kwargs)
        self.artist = self._get('artist', **kwargs)
        self.album = self._get('album', **kwargs)
        self.total_time = self._get('total_time', **kwargs)
        self.position = self._get('position', **kwargs)
        self.mediakind = self._get('mediakind', **kwargs)
        self.playstatus = self._get('playstatus', **kwargs)

    def _get(self, name, **kwargs):
        if name in kwargs:
            return kwargs[name]
        else:
            return None


class FakeAppleTV(web.Application):
    """Implementation of fake Apple TV."""

    def __init__(self, loop, hsgid, session_id, testcase):
        """Initialize a new FakeAppleTV."""
        super().__init__(loop=loop)
        self.responses = {}
        self.responses['login'] = LoginResponse(session_id, 200)
        self.responses['artwork'] = None
        self.responses['playing'] = PlayingResponse()
        self.hsgid = hsgid
        self.last_button_pressed = None
        self.tc = testcase

        self.router.add_get('/login', self.handle_login)
        self.router.add_get(
            '/ctrl-int/1/playstatusupdate', self.handle_playstatus)
        self.router.add_post(
            '/ctrl-int/1/controlpromptentry', self.handle_remote_button)
        self.router.add_get(
            '/ctrl-int/1/nowplayingartwork', self.handle_artwork)
        self.router.add_post(
            '/ctrl-int/1/setproperty', self.handle_set_property)
        for button in ['play', 'pause', 'nextitem', 'previtem']:
            self.router.add_post('/ctrl-int/1/' + button,
                                 self.handle_playback_button)

    @asyncio.coroutine
    def handle_login(self, request):
        """Handler for login requests."""
        self._verify_headers(request)
        self._verify_auth_parameters(request, check_session=False)
        data = self.responses['login']
        mlid = tags.uint32_tag('mlid', data.session)
        mlog = tags.container_tag('mlog', mlid)
        return web.Response(body=mlog, status=data.status)

    @asyncio.coroutine
    def handle_playback_button(self, request):
        """Handler for playback buttons."""
        self._verify_auth_parameters(request)
        self.last_button_pressed = request.rel_url.path.split('/')[-1]
        return web.Response(status=200)

    @asyncio.coroutine
    def handle_remote_button(self, request):
        """Handler for remote control buttons."""
        self._verify_auth_parameters(request)
        content = yield from request.content.read()
        parsed = dmap.parse(content, tag_definitions.lookup_tag)
        self.last_button_pressed = dmap.first(parsed, 'cmbe')
        return web.Response(status=200)

    @asyncio.coroutine
    def handle_artwork(self, request):
        """Handler for artwork requests."""
        self._verify_auth_parameters(request)
        artwork = self.responses['artwork']
        return web.Response(body=artwork, status=200)

    @asyncio.coroutine
    def handle_playstatus(self, request):
        """Handler for playstatus (currently playing) requests."""
        self._verify_auth_parameters(request)

        body = b''
        playing = self.responses['playing']

        if playing.paused is not None:
            body += tags.uint32_tag('caps', 3 if playing.paused else 4)

        if playing.title is not None:
            body += tags.string_tag('cann', playing.title)

        if playing.artist is not None:
            body += tags.string_tag('cana', playing.artist)

        if playing.album is not None:
            body += tags.string_tag('canl', playing.album)

        if playing.total_time is not None:
            total_time = playing.total_time * 1000  # sec -> ms
            body += tags.uint32_tag('cast', total_time)

            if playing.position is not None:
                pos = (playing.total_time - playing.position)
                body += tags.uint32_tag('cant', pos*1000)  # sec -> ms

        if playing.mediakind is not None:
            body += tags.uint32_tag('cmmk', playing.mediakind)

        if playing.playstatus is not None:
            body += tags.uint32_tag('caps', playing.playstatus)

        return web.Response(
            body=tags.container_tag('cmst', body), status=200)

    @asyncio.coroutine
    def handle_set_property(self, request):
        """Handler for property changes."""
        self._verify_auth_parameters(request)
        self.tc.assertIn('dacp.playingtime', request.rel_url.query,
                         msg='property to set is missing')
        playingtime = request.rel_url.query['dacp.playingtime']
        self.responses['playing'].position = int(playingtime)
        return web.Response(body=b'', status=200)

    # Verifies that all needed headers are included in the request. Should be
    # checked in all requests, but that seems a bit too much and not that
    # necessary.
    def _verify_headers(self, request):
        for header in EXPECTED_HEADERS:
            self.tc.assertIn(header, request.headers)
            self.tc.assertEqual(request.headers[header],
                                EXPECTED_HEADERS[header])

    # This method makes sure that the correct hsgid and/or session id is
    # included in the GET-parameters. As this is extremely important for
    # anything to work, this should be verified in all requests.
    def _verify_auth_parameters(self, request,
                                check_hsgid=True,
                                check_session=True):
        params = request.rel_url.query
        if check_hsgid:
            self.tc.assertEqual(params['hsgid'], self.hsgid,
                                msg='hsgid does not match')

        if check_session:
            session = self.responses['login'].session
            self.tc.assertEqual(int(params['session-id']), session,
                                msg='session id does not match')


class AppleTVUseCases:
    """Wrapper for altering behavior of a FakeAppleTV instance.

    Extend and use this class to alter behavior of a fake Apple TV device.
    """

    def __init__(self, fake_apple_tv):
        """Initialize a new AppleTVUseCases."""
        self.device = fake_apple_tv

    def make_login_fail(self):
        """Calling this method will make login fail with response 503."""
        self.device.responses['login'] = LoginResponse(0, 503)

    def change_artwork(self, artwork):
        """Calling this method will change artwork response."""
        self.device.responses['artwork'] = artwork

    def nothing_playing(self):
        """Calling this method will put device in idle state."""
        self.device.responses['playing'] = PlayingResponse()

    def video_playing(self, paused, title, total_time, position):
        """Calling this method changes what is currently plaing to video."""
        self.device.responses['playing'] = PlayingResponse(
            paused=paused, title=title,
            total_time=total_time, position=position,
            mediakind=3)

    def music_playing(self, paused, artist, album, title,
                      total_time, position):
        """Calling this method changes what is currently plaing to music."""
        self.device.responses['playing'] = PlayingResponse(
            paused=paused, title=title,
            artist=artist, album=album,
            total_time=total_time,
            position=position,
            mediakind=2)

    def media_is_loading(self):
        """Calling this method puts device in a loading state."""
        self.device.responses['playing'] = PlayingResponse(
            playstatus=1)
