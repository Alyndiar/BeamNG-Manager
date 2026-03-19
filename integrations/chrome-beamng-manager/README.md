# Chrome BeamNG-Manager Bridge (MVP)

This extension shows `Subscribed` and `Manually Installed` highlights/badges on:

- `https://www.beamng.com/resources/*`
- `https://www.beamng.com/forums/*`

It uses the same localhost bridge protocol as the Firefox extension:

- `GET /session/start`
- `GET /changes`
- `GET /markers`
- `GET /commands/next`

When BeamNG-Manager queues an open command, the extension drains it from `/commands/next` and opens a new tab.

## Install (official listing)

- Chrome Web Store: <https://chromewebstore.google.com/detail/hhilmajldhikjnjeafjfihpnkmbodggh>

## Install (developer/unpacked)

1. Open Chrome `chrome://extensions`.
2. Enable `Developer mode`.
3. Click `Load unpacked`.
4. Select this folder.

## Port configuration

Set the same bridge port in both places:

- BeamNG-Manager: `Settings -> Browser Bridge Port`
- Extension popup/options: `Bridge port`

Polling interval is configurable in popup/options (4-60s), but Chromium service-worker alarm cadence may effectively floor near 30s.

## Notes

- This is an MVP focused on BeamNG repo/forums pages.
- Communication is local-only over loopback.
