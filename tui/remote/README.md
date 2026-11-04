# Terminal remote

Run `bin/thermite remote --mock` to explore a fictional house. Node.js and the locked blessed package are required.

Home shows your rooms and opens Remotes, Security, Cameras or Site. Use the arrow keys to choose, Enter to open, and `?` for the current page's keys. `Tab` changes panes, `PgUp`/`PgDn` scroll, `t` changes theme, and `q` quits. Pair devices with `F6` from Remotes.

For a real house, sign in with `thermite access login --url https://thermite.example:13600 --user operator`, then run `thermite remote --url https://thermite.example:13600`. Use your own host and account. After changing inventory, run `thermite generate`; the terminal uses your site's generated house model. Credentials stay in an owner-only file.
