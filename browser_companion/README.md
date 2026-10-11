# AbyssBeacon Firefox Download Companion

Optional helper for CivitAI Red downloads that require browser verification.
Requires desktop Firefox 140 or later. Chrome and Edge are not supported by this package.

## Install

Open Firefox's Add-ons Manager (about:addons), choose the gear menu, then
Install Add-on From File. Select abyssbeaconfirefoxhelper-0.2.0.xpi beside this
README and approve the requested permissions. Restart Firefox.

On Windows, Firefox must be your default browser for the current browser handoff.
Sign in to CivitAI Red in that Firefox profile if the website requires it.

Start the download in AbyssBeacon. If verification is required, it opens your
browser. Complete any verification there. The helper hands the selected file
back to AbyssBeacon, which uses your configured destination and Download Manager.
Regular downloads started on the website remain ordinary browser downloads.

## Privacy and updates

The helper does not read login cookies or passwords. It passes the selected
file's temporary signed download URL to AbyssBeacon on localhost. It does not
send that URL to the extension developer or an analytics service. Optional
technical diagnostics go only to the local app when the corresponding Firefox
permission is granted.

This is Mozilla's signed v0.2.0 package. Renaming it does not change its contents.
No automatic update feed is configured; install future signed releases manually.
The firefox folder contains the matching plain JavaScript source.
