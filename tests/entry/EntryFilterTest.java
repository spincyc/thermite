import java.lang.reflect.Proxy;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.Map;

import javax.servlet.FilterChain;
import javax.servlet.RequestDispatcher;
import javax.servlet.http.HttpServletRequest;
import javax.servlet.http.HttpServletResponse;

import org.thermite.entry.EntryFilter;

/** Exercises the shipped class, with servlet-interface stand-ins and no HTTP server. */
public final class EntryFilterTest {
    private static final Map<String, String> PAGES = new LinkedHashMap<>();

    private static void request(String method, String path, String query, String action) throws Exception {
        int[] status = {0};
        int[] called = {0};
        int[] forwarded = {0};
        Map<String, String> headers = new HashMap<>();
        String[] target = {null};
        HttpServletRequest req = (HttpServletRequest) Proxy.newProxyInstance(
                EntryFilterTest.class.getClassLoader(), new Class<?>[] {HttpServletRequest.class},
                (proxy, name, args) -> {
                    switch (name.getName()) {
                        case "getMethod": return method;
                        case "getRequestURI": return path;
                        case "getQueryString": return query;
                        case "getRequestDispatcher":
                            target[0] = (String) args[0];
                            return Proxy.newProxyInstance(EntryFilterTest.class.getClassLoader(),
                                    new Class<?>[] {RequestDispatcher.class}, (dispatcher, operation, values) -> {
                                        if (!"forward".equals(operation.getName())) throw new AssertionError("Not a forward");
                                        forwarded[0]++;
                                        return null;
                                    });
                        default: throw new AssertionError("Unexpected request access: " + name.getName());
                    }
                });
        HttpServletResponse res = (HttpServletResponse) Proxy.newProxyInstance(
                EntryFilterTest.class.getClassLoader(), new Class<?>[] {HttpServletResponse.class},
                (proxy, name, args) -> {
                    switch (name.getName()) {
                        case "setStatus": case "sendError": status[0] = (Integer) args[0]; return null;
                        case "setHeader": headers.put((String) args[0], (String) args[1]); return null;
                        default: throw new AssertionError("Unexpected response access: " + name.getName());
                    }
                });
        FilterChain chain = (request, response) -> {
            if (request != req || response != res) throw new AssertionError("Wrapped downstream request");
            called[0]++;
        };
        new EntryFilter(PAGES).doFilter(req, res, chain);
        if ("redirect".equals(action)) {
            if (called[0] != 0 || forwarded[0] != 0 || status[0] != 302 || headers.size() != 2
                    || !(path.startsWith("/overview") ? "/" : path + "/").equals(headers.get("Location"))
                    || !"no-store".equals(headers.get("Cache-Control"))) {
                throw new AssertionError("Incorrect redirect: " + method + " " + path);
            }
        } else if ("forward".equals(action)) {
            String asset = PAGES.get(path);
            if (called[0] != 0 || forwarded[0] != 1 || status[0] != 0 || headers.size() != 2
                    || !("/static/remote/" + asset).equals(target[0])
                    || !asset.substring(0, asset.length() - 5).equals(headers.get("X-Thermite-Page"))
                    || !"no-store".equals(headers.get("Cache-Control")) || headers.containsKey("Location")) {
                throw new AssertionError("Incorrect forward: " + method + " " + path);
            }
        } else if ("missing".equals(action)) {
            if (called[0] != 0 || forwarded[0] != 0 || status[0] != 503 || headers.size() != 1
                    || !"no-store".equals(headers.get("Cache-Control"))) throw new AssertionError("Missing asset fallback");
        } else if (called[0] != 1 || forwarded[0] != 0 || status[0] != 0 || !headers.isEmpty()) {
            throw new AssertionError("Changed a native request: " + method + " " + path);
        }
    }

    public static void main(String[] args) throws Exception {
        System.setProperty("openhab.conf", args[0]);
        Path assets = Path.of(args[0], "html", "remote");
        Files.createDirectories(assets);
        for (String line : Files.readAllLines(Path.of(args[1]))) {
            String[] pair = line.split("\t");
            PAGES.put(pair[0], pair[1]);
            Files.writeString(assets.resolve(pair[1]), "<html>Thermite fixture</html>");
        }
        for (String path : PAGES.keySet()) {
            request("GET", path, null, "forward");
            request("HEAD", path, "x=1", "forward");
            request("POST", path, null, "pass");
            request("OPTIONS", path, null, "pass");
            if (!"/".equals(path)) {
                String alias = path.substring(0, path.length() - 1);
                request("GET", alias, "next=https://foreign.invalid&token=private", "redirect");
                request("HEAD", alias, null, "redirect");
                request("POST", alias, null, "pass");
            }
        }
        for (String path : new String[] {"/overview", "/overview/"}) {
            request("GET", path, "code=x&state=y", "redirect");
            request("HEAD", path, null, "redirect");
            request("POST", path, null, "pass");
        }
        for (String query : new String[] {"code=opaque&state=opaque", "error=denied&state=x", "state",
                "x=1&c%6fde=x", "s%74ate=x", "error=x", "code="}) {
            request("GET", "/", query, "pass");
        }
        for (String query : new String[] {"next=https://example.invalid", "decode=x", "x=state", "bad%zz=x",
                "abcdefghijklmnopqrstuvwxyz0123456789=x&x=1", "x=1&&", ""}) {
            request("GET", "/", query, "forward");
        }
        for (String path : new String[] {"/settings/", "/auth", "/createApiToken", "/changePassword",
                "/rest/", "/rest/events", "/ws", "/static/remote/home.html", "/overview/child", "/overview%2f"}) {
            request("GET", path, null, "pass");
            request("POST", path, null, "pass");
        }
        Files.delete(assets.resolve("home.html"));
        request("GET", "/", null, "missing");
        Files.createSymbolicLink(assets.resolve("home.html"), assets.resolve("index.html"));
        request("GET", "/", null, "missing");
        for (Map<String, String> invalid : new Map[] {Map.of("/", "../secret.html"), Map.of("/../", "home.html")}) {
            try {
                new EntryFilter(invalid);
                throw new AssertionError("Accepted invalid fixed mapping");
            } catch (IllegalArgumentException expected) {
                // Only generated finite mappings can become HTTP routes.
            }
        }
        System.out.println("entry filter clean forwards, OAuth callbacks and native request preservation passed");
    }
}
