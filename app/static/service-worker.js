const STATIC_CACHE = "termin-static-v2";
const SHELL_CACHE = "termin-shell-v2";
const FAVICON_CACHE = "termin-favicons-v2";
const faviconInflight = new Map();
const STATIC_ASSETS = [
  "/manifest.webmanifest",
  "/static/brand/termin-icon-v4-192.png",
  "/static/brand/termin-icon-v4-512.png",
  "/static/brand/termin-icon-v4-180.png",
];

function isGet(request) {
  return request && request.method === "GET";
}

function isHttp(request) {
  return request && request.url && request.url.startsWith("http");
}

function isSameOrigin(request) {
  try {
    return new URL(request.url).origin === self.location.origin;
  } catch (_error) {
    return false;
  }
}

function isApiRequest(url) {
  return url.pathname.startsWith("/api/");
}

function isSocketRequest(url) {
  return url.pathname.startsWith("/socket.io/");
}

function isServiceWorkerRequest(url) {
  return url.pathname === "/service-worker.js";
}

function isLinkFaviconRequest(url) {
  return url.pathname === "/link-favicon";
}

function normalizedLinkFaviconUrl(url) {
  if (!isLinkFaviconRequest(url)) {
    return url.toString();
  }
  const rawTarget = url.searchParams.get("url") || "";
  try {
    const parsedTarget = new URL(rawTarget);
    if (parsedTarget.protocol !== "http:" && parsedTarget.protocol !== "https:") {
      return url.toString();
    }
    const normalized = new URL(url.toString());
    normalized.searchParams.set("url", parsedTarget.origin);
    return normalized.toString();
  } catch (_error) {
    return url.toString();
  }
}

function isNavigationRequest(request) {
  return request.mode === "navigate";
}

function isStaticAsset(url) {
  return (
    url.pathname.startsWith("/static/") ||
    url.pathname === "/manifest.webmanifest" ||
    url.pathname === "/favicon.ico"
  );
}

function isShellPath(url) {
  if (url.pathname.startsWith("/debug/")) {
    return false;
  }
  if (url.pathname === "/" || url.pathname === "/dashboard" || url.pathname === "/todo" || url.pathname === "/inbox") {
    return true;
  }
  if (url.pathname.startsWith("/tree/")) {
    return true;
  }
  if (url.pathname.startsWith("/task/")) {
    return true;
  }
  return false;
}

async function staleWhileRevalidate(cacheName, request) {
  const cache = await caches.open(cacheName);
  const cached = await cache.match(request);
  const networkPromise = fetch(request)
    .then(function (response) {
      if (response && response.ok) {
        cache.put(request, response.clone());
      }
      return response;
    })
    .catch(function () {
      return null;
    });
  return cached || networkPromise || fetch(request);
}

async function cacheFirst(cacheName, request) {
  const cache = await caches.open(cacheName);
  const requestUrl = new URL(request.url);
  const normalizedFaviconUrl = isLinkFaviconRequest(requestUrl) ? normalizedLinkFaviconUrl(requestUrl) : "";
  const cacheKey = normalizedFaviconUrl
    ? new Request(normalizedFaviconUrl, { method: "GET", credentials: "same-origin" })
    : request;
  const cached = await cache.match(cacheKey);
  if (cached) {
    return cached;
  }
  if (normalizedFaviconUrl && faviconInflight.has(normalizedFaviconUrl)) {
    return faviconInflight.get(normalizedFaviconUrl).then(function (response) {
      return response.clone();
    });
  }
  const networkPromise = fetch(normalizedFaviconUrl || request)
    .then(function (response) {
      if (response && response.ok) {
        cache.put(cacheKey, response.clone());
      }
      return response;
    })
    .finally(function () {
      if (normalizedFaviconUrl) {
        faviconInflight.delete(normalizedFaviconUrl);
      }
    });
  if (normalizedFaviconUrl) {
    faviconInflight.set(normalizedFaviconUrl, networkPromise);
  }
  const response = await networkPromise;
  return normalizedFaviconUrl && response ? response.clone() : response;
}

async function networkFirst(cacheName, request) {
  const cache = await caches.open(cacheName);
  try {
    const response = await fetch(request);
    if (response && response.ok) {
      cache.put(request, response.clone());
    }
    return response;
  } catch (_error) {
    const cached = await cache.match(request);
    if (cached) return cached;
    throw _error;
  }
}

self.addEventListener("install", function (event) {
  event.waitUntil(
    caches.open(STATIC_CACHE).then(function (cache) {
      return cache.addAll(STATIC_ASSETS);
    }).catch(function () {
      return null;
    })
  );
  self.skipWaiting();
});

self.addEventListener("activate", function (event) {
  event.waitUntil(
    caches.keys().then(function (keys) {
      return Promise.all(
        keys.map(function (key) {
          if (key === STATIC_CACHE || key === SHELL_CACHE || key === FAVICON_CACHE) return null;
          return caches.delete(key);
        })
      );
    }).then(function () {
      return self.clients.claim();
    })
  );
});

self.addEventListener("fetch", function (event) {
  const request = event.request;
  if (!isGet(request) || !isHttp(request) || !isSameOrigin(request)) {
    return;
  }

  const url = new URL(request.url);
  if (isApiRequest(url) || isSocketRequest(url) || isServiceWorkerRequest(url)) {
    return;
  }

  if (url.pathname.startsWith("/debug/")) {
    return;
  }

  if (isLinkFaviconRequest(url)) {
    event.respondWith(cacheFirst(FAVICON_CACHE, request));
    return;
  }

  if (isStaticAsset(url)) {
    event.respondWith(staleWhileRevalidate(STATIC_CACHE, request));
    return;
  }

  if (isNavigationRequest(request) && isShellPath(url)) {
    event.respondWith(networkFirst(SHELL_CACHE, request));
    return;
  }
});

self.addEventListener("push", function (event) {
  let payload = {};
  try {
    payload = event.data ? event.data.json() : {};
  } catch (_error) {
    payload = {};
  }

  const title = payload.title || "Termin";
  const options = {
    body: payload.body || "You have a new update.",
    icon: "/static/brand/termin-icon-v4-192.png",
    badge: "/static/brand/termin-icon-v4-192.png",
    tag: payload.tag || "termin-notification",
    timestamp: Number(payload.timestamp) || Date.now(),
    data: {
      url: payload.url || "/",
    },
  };

  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener("notificationclick", function (event) {
  event.notification.close();
  const targetUrl = (event.notification.data && event.notification.data.url) || "/";
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then(function (clients) {
      for (const client of clients) {
        if ("focus" in client) {
          client.navigate(targetUrl);
          return client.focus();
        }
      }
      if (self.clients.openWindow) {
        return self.clients.openWindow(targetUrl);
      }
      return null;
    })
  );
});
