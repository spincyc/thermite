# Terminal remote

Run `bin/thermite remote --mock` to explore a fictional house. Node.js and the locked blessed package are required.

Home shows your rooms and opens Remotes, Security, Cameras or Site. Use the arrow keys to choose, Enter to open, and `?` for the current page's keys. `Tab` changes panes, `PgUp`/`PgDn` scroll, `t` changes theme, and `q` quits. Pair devices with `F6` from Remotes.

For a real house, run `thermite remote --url https://thermite.example:13600`. Basic controls in unlocked rooms work without a login in either authentication mode. Locked rooms retain their remote layout and show a lock indicator with controls disabled. To use an account's management permissions, sign in with `thermite access login --url https://thermite.example:13600 --user operator`. Use your own host and account. After changing inventory, run `thermite generate`; the terminal uses your site's generated house model. Credentials stay in an owner-only file.

With authentication enabled, administrators can lock the current room directly from Remotes with `K` or the **Lock room** button. The server captures its current reported source or Off and holds it until manually unlocked, including across restarts. The manual lock takes precedence over schedules; releasing it resumes any current schedule. Other users see the lock status without an unlock control.

Security (F3) shows authentication status and the browser address for changing it. First setup uses a short-lived code generated on the host by `thermite access setup --config /path/to/access/config.json`, run from the staged release as the thermite service account. Changing authentication signs out every browser and terminal. Pairing, locks and room schedules require authentication. To elevate access, quit, sign in as an administrator with `thermite access login`, and reopen the terminal; an administrator can also change a user's role in Users & roles, after which that user must sign in again. Explicit native testing mode cannot change authentication or secure direct runtime access.
