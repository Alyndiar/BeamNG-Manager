# Signing & Persistent Install (Firefox)

This extension can be installed permanently only as a **signed** `.xpi`.

## 1. Build unsigned XPI

From this folder:

```powershell
.\package_xpi.ps1
```

Output:

- `dist/beamng-manager-bridge-<version>-unsigned.xpi`

## 2. Submit for signing (AMO, Unlisted)

1. Create/login to AMO developer account: <https://addons.mozilla.org/developers/>
2. Choose to submit a new add-on.
3. Pick **Unlisted** distribution.
4. Upload the unsigned `.xpi`.
5. Complete any metadata prompts and wait for signing.
6. Download the signed `.xpi` from AMO.

## 3. Install signed XPI permanently

1. Open Firefox `about:addons`.
2. Click the gear icon.
3. Choose **Install Add-on From File...**
4. Select the signed `.xpi`.

The add-on now persists across restarts.

## 4. Port configuration

Set the same port in both places:

- BeamNG-Manager: `Settings -> Browser Bridge Port`
- Firefox add-on: `about:addons -> BeamNG-Manager Bridge -> Preferences`

## Notes

- If AMO reports schema/signing issues, bump `manifest.json` version and rebuild XPI.
- Keep extension ID stable (`beamng-manager-bridge@local`) for update continuity.
- AMO listing metadata reference: default language is English (Canadian), with an additional French description.
