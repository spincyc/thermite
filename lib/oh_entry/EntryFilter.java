package org.thermite.entry;

import java.io.IOException;
import java.net.URLDecoder;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.LinkOption;
import java.nio.file.Path;
import java.util.Map;

import javax.servlet.Filter;
import javax.servlet.FilterChain;
import javax.servlet.FilterConfig;
import javax.servlet.ServletException;
import javax.servlet.ServletRequest;
import javax.servlet.ServletResponse;
import javax.servlet.http.HttpServletRequest;
import javax.servlet.http.HttpServletResponse;

/** Presentation routing only. Native authentication and API authority are unchanged. */
public final class EntryFilter implements Filter {
    private final Map<String, String> pages;
    private final Path assets;

    public EntryFilter(Map<String, String> pages) {
        for (Map.Entry<String, String> entry : pages.entrySet()) {
            if (!entry.getKey().matches("/(?:[a-z][a-z-]*/)?")
                    || !entry.getValue().matches("[a-z][a-z0-9-]*\\.html")) {
                throw new IllegalArgumentException("Invalid Thermite page mapping");
            }
        }
        this.pages = Map.copyOf(pages);
        this.assets = Path.of(System.getProperty("openhab.conf"), "html", "remote");
    }

    @Override
    public void init(FilterConfig config) {
    }

    @Override
    public void destroy() {
    }

    private static boolean isOAuthCallback(String query) {
        if (query == null) {
            return false;
        }
        // Inspect names only: never decode, copy, log, or redirect an OAuth code.
        // Jetty bounds the request line. Each name allocation is bounded as well.
        int start = 0;
        while (start < query.length()) {
            int end = query.indexOf('&', start);
            if (end < 0) {
                end = query.length();
            }
            int nameEnd = start;
            while (nameEnd < end && nameEnd - start <= 32 && query.charAt(nameEnd) != '=') {
                nameEnd++;
            }
            if (nameEnd - start <= 32) {
                try {
                    String name = URLDecoder.decode(query.substring(start, nameEnd), StandardCharsets.UTF_8);
                    if ("code".equals(name) || "state".equals(name) || "error".equals(name)) {
                        return true;
                    }
                } catch (IllegalArgumentException malformedEncoding) {
                    // An unrelated malformed name must not turn a normal visit into an error.
                }
            }
            start = end + 1;
        }
        return false;
    }

    @Override
    public void doFilter(ServletRequest request, ServletResponse response, FilterChain chain)
            throws IOException, ServletException {
        if (request instanceof HttpServletRequest && response instanceof HttpServletResponse) {
            HttpServletRequest http = (HttpServletRequest) request;
            String method = http.getMethod();
            String path = http.getRequestURI();
            boolean root = "/".equals(path);
            if ("GET".equals(method) || "HEAD".equals(method)) {
                HttpServletResponse page = (HttpServletResponse) response;
                String canonical = "/overview".equals(path) || "/overview/".equals(path) ? "/"
                        : pages.containsKey(path + "/") ? path + "/" : null;
                if (canonical != null) {
                    page.setStatus(HttpServletResponse.SC_FOUND);
                    page.setHeader("Location", canonical);
                    page.setHeader("Cache-Control", "no-store");
                    return;
                }
                String asset = pages.get(path);
                if (asset != null && !(root && isOAuthCallback(http.getQueryString()))) {
                    page.setHeader("Cache-Control", "no-store");
                    if (!Files.isRegularFile(assets.resolve(asset), LinkOption.NOFOLLOW_LINKS)) {
                        page.sendError(HttpServletResponse.SC_SERVICE_UNAVAILABLE);
                        return;
                    }
                    page.setHeader("X-Thermite-Page", asset.substring(0, asset.length() - 5));
                    http.getRequestDispatcher("/static/remote/" + asset).forward(request, response);
                    return;
                }
            }
        }
        chain.doFilter(request, response);
    }
}
