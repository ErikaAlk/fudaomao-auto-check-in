import hashlib
from pathlib import Path

from aiohttp import web
from homeassistant.components import panel_custom
from homeassistant.components.http import HomeAssistantView, StaticPathConfig
from homeassistant.helpers.aiohttp_client import async_get_clientsession

DOMAIN = 'lzu_attendance'


async def async_setup(hass, config):
    options = config[DOMAIN]
    resource_version = hashlib.sha256((Path(__file__).parent / 'www/panel.js').read_bytes()).hexdigest()[:16]
    hass.http.register_view(AttendanceView(hass, options))
    await hass.http.async_register_static_paths([
        StaticPathConfig('/lzu_attendance_static', str(Path(__file__).parent / 'www'), False)
    ])
    await panel_custom.async_register_panel(
        hass, frontend_url_path='attendance', webcomponent_name='lzu-attendance-panel',
        sidebar_title='签到', sidebar_icon='mdi:clipboard-check-outline',
        module_url=f'/lzu_attendance_static/panel.js?v={resource_version}', require_admin=True)
    return True


class AttendanceView(HomeAssistantView):
    url = '/api/lzu_attendance'
    name = 'api:lzu_attendance'
    requires_auth = True

    def __init__(self, hass, options):
        self.hass = hass
        self._options = options

    async def forward(self, request, method, endpoint, payload=None):
        if not request['hass_user'].is_admin:
            raise web.HTTPForbidden(text='此功能仅限管理员使用')
        session = async_get_clientsession(self.hass)
        async with session.request(method, self._options['url'].rstrip('/') + endpoint,
                                   json=payload, headers={'Authorization': 'Bearer ' + self._options['token']},
                                   timeout=30, allow_redirects=False) as response:
            data = await response.json()
            return self.json(data, status_code=response.status, headers={'Cache-Control': 'no-store'})

    async def get(self, request):
        return await self.forward(request, 'GET', '/api/state')

    async def post(self, request):
        if request.content_length and request.content_length > 16384:
            raise web.HTTPRequestEntityTooLarge(max_size=16384, actual_size=request.content_length)
        return await self.forward(request, 'POST', '/api/action', await request.json())
