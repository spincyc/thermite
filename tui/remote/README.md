# Terminal remote

Run `bin/thermite remote --mock` to explore a fictional house. Node.js and the locked blessed package are required.

Home shows your rooms and opens Remotes, Security, Cameras or Site. Use the arrow keys to choose, Enter to open, and `?` for the current page's keys. `Tab` changes panes, `PgUp`/`PgDn` scroll, `t` changes theme, and `q` quits. Pair devices with `F6` from Remotes.

For a real house, run `thermite remote --url https://thermite.example:13600`. The terminal reads the gateway's authentication setting: when authentication is off, basic remote controls work without a login. When it is on, first sign in with `thermite access login --url https://thermite.example:13600 --user operator`. Use your own host and account. After changing inventory, run `thermite generate`; the terminal uses your site's generated house model. Credentials stay in an owner-only file.

Security (F3) shows authentication status and the browser address for changing it. First setup uses a short-lived code generated on the host by `thermite access setup --config /path/to/access/config.json`, run from the staged release as the thermite service account. Changing authentication signs out every browser and terminal. Pairing, locks and room schedules require authentication. To elevate access, quit, sign in as an administrator with `thermite access login`, and reopen the terminal; an administrator can also change a user's role in Users & roles, after which that user must sign in again. Explicit native testing mode cannot change authentication or secure direct runtime access.
