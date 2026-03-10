# Firefox BeamNG-Manager Bridge (MVP)

This extension shows `Subscribed` and `Manually Installed` highlights/badges on:

- `https://www.beamng.com/resources/*`
- `https://www.beamng.com/forums/*`

The extension auto-activates only when BeamNG-Manager is running and its local bridge is reachable.
If BeamNG-Manager is not running, the extension does nothing.

## Install (temporary)

1. Open Firefox and go to `about:debugging#/runtime/this-firefox`.
2. Click `Load Temporary Add-on...`.
3. Select `manifest.json` from this folder.

## Install (persistent)

See [SIGNING.md](/W:/Dany/BeamNG-Manager/integrations/firefox-beamng-manager/SIGNING.md).

## How it connects

- Extension starts a local bridge session with `GET /session/start`.
- It polls `GET /changes` and only fetches deltas.
- It fetches markers with `GET /markers` when marker data changed.
- It drains open-URL commands with `GET /commands/next`.
- Default preferred port is `49441`.
- BeamNG-Manager can fall back to nearby ports if preferred port is busy.
- Extension scans a small port range starting at the preferred/default port.
- You can set an explicit extension port in `about:addons` -> extension details -> `Preferences`.
- You can also click the extension toolbar icon and set the port directly in the popup.
- Poll interval is configurable (4-60 seconds) in popup/options.

To avoid mismatch, set the same port in:

- BeamNG-Manager: `Settings -> Browser Bridge Port`
- Firefox extension options: `Bridge port`

## Notes

- This is an MVP and currently focused on BeamNG repo/forums pages.
- No data is sent to external services; communication is local loopback only.
