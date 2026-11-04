# Native Thermite entry

`files()` generates the private helper JAR under
`automation/js/node_modules/thermite-entry/` and its activating script. Both
are deployed with the ordinary generated configuration; the helper sorts
before the script. The script embeds the helper's exact SHA-256, so a helper
change also reloads the script. Installation needs the existing JavaScript
Scripting add-on, with no additional daemon, listener or compiler.

The script registers a pure Java Servlet Filter through the supported OSGi
HTTP Whiteboard service. Jetty calls Java directly. JavaScript runs only at
registration and disposal, where it unregisters the service before closing
the private classloader. No timer, request polling or JavaScript HTTP callback
is involved. The default Whiteboard context matches pinned MainUI's default
context selection. Exact mappings and the Java path check leave native API,
authentication, event streams and other applications alone.

GET and HEAD visits to the finite routes in `oh_routes.py` are forwarded
inside the native servlet context to their fixed HTML assets. The browser
keeps the clean path (`/`, `/remotes/`, `/system/`, etc.). Generated HTML uses
absolute `/static/remote/` asset URLs. Slashless page aliases redirect to their
canonical trailing-slash paths; legacy `/overview` and `/overview/` redirect
to `/`. These temporary redirects do not copy query parameters. Root OAuth
callbacks with `code`, `state` or `error` parameter names pass through to
MainUI. Parameter values are never decoded or copied into a redirect.
Other methods pass through. The deliberate native entry remains `/settings/`;
Thermite room settings use `/room-settings/`.
This is presentation routing; native listener/authentication policy remains
the authority. A failed or unloaded helper restores upstream routing, so
installation readiness must verify the Home response before reporting success.

`ready(url, timeout)` makes one direct HEAD request to each of `/` and
`/overview/`, under one total deadline. It accepts only a literal native
loopback HTTP origin, sends no credentials, follows no redirects, uses no
proxy and reads no bodies. Home must return 200, `X-Thermite-Page: home` and
`no-store`; legacy overview must redirect exactly to `/` with `no-store`.
The Java filter verifies each fixed HTML asset is a regular file before
forwarding and responds 503 for a missing or symlinked asset. This prevents a
MainUI fallback response from falsely proving readiness. Page mappings are
copied into an immutable Java map before service registration.

`EntryFilter.java` is the reviewed source. `filter.jar` is its deterministic
compiled asset, with exact source/artifact/compiler/Servlet API hashes in
`inputs.json`. `checked_artifact()` refuses source or binary drift before
generation. `approved_artifacts()` declares the single source-controlled
binary exception for export validation. The JAR contains only its manifest
and one class, has fixed ZIP timestamps/permissions, and uses stored entries.
It is not runtime state and contains no instance facts or credentials.

The explicit rebuild/behavior gate uses cached, verified inputs and never
downloads or installs anything:

```sh
THERMITE_JAVA=/absolute/pinned-jre/bin/java \
THERMITE_ECJ=/absolute/ecj-3.41.0.jar \
THERMITE_RUNTIME=/absolute/openhab-5.2.3/runtime \
PYTHONPATH=lib python3 -B -m unittest tests.test_entry -v
```

The gate rebuilds the JAR and compares every byte before exercising the
shipped Java class. The ordinary tests check packaging/integrity and script
registration/disposal without launching openHAB. Native integration must also
check real MainUI context matching, callback passthrough and script reload.

Pinned primary contracts:

- [MainUI UIServlet](https://github.com/openhab/openhab-webui/blob/5.2.3/bundles/org.openhab.ui/src/main/java/org/openhab/ui/internal/UIServlet.java)
  owns `/*`; ordinary configuration files are served under `/static`.
- [MainUI OAuth](https://github.com/openhab/openhab-webui/blob/5.2.3/bundles/org.openhab.ui/web/src/js/openhab/auth.ts)
  uses the origin root for its OAuth redirect.
- [Pax Web 8.0.35 filter tracker](https://github.com/ops4j/org.ops4j.pax.web/blob/web-8.0.35/pax-web-extender-whiteboard/src/main/java/org/ops4j/pax/web/extender/whiteboard/internal/tracker/FilterTracker.java)
  reads Whiteboard filter mappings, dispatcher type and async support.
- [Pax Web context selection](https://github.com/ops4j/org.ops4j.pax.web/blob/web-8.0.35/pax-web-extender-whiteboard/src/main/java/org/ops4j/pax/web/extender/whiteboard/internal/tracker/AbstractElementTracker.java)
  defaults unselected Whiteboard services to the shared `default` context.
- [Jetty 9.4.58 RegexRule](https://github.com/jetty/jetty.project/blob/jetty-9.4.58.v20250814/jetty-rewrite/src/main/java/org/eclipse/jetty/rewrite/handler/RegexRule.java)
  matches only the path target. A stock XML redirect also redirects OAuth
  root callbacks, so it cannot implement this routing contract alone.
