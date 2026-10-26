# Security and privacy

Google Takeout archives, generated orphan reports, deletion journals, and
Google Photos permalinks are private account data. Do not commit or share them.
The repository ignore rules cover the standard filenames, but always inspect
`git status` before committing.

The deletion tool never stores Google credentials and never automates sign-in.
It connects to a Chrome instance that you start and authenticate yourself.
Chrome's remote-debugging port provides powerful control over that browser
profile:

- bind it only to the local machine;
- use a dedicated Chrome profile;
- do not expose port 9222 to a network;
- close that Chrome instance when cleanup is finished.

If a report or journal is exposed, treat its photo permalinks and metadata as
private information and remove the exposed artifact. If a browser profile is
exposed, review active Google sessions and revoke access as appropriate.

Please report software vulnerabilities through a private GitHub Security
Advisory. Do not attach Takeout data, reports, journals, cookies, or screenshots
containing personal photos.
