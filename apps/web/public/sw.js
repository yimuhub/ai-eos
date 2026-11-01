/**
 * AI-EOS 移动端 Service Worker（最小可用）
 *
 * - 导航请求 network-first，失败回退缓存的 /m 外壳（离线时至少能看到界面）；
 * - 静态资源 stale-while-revalidate；
 * - 绝不缓存接口：审批/验收数据必须实时，缓存会造成"看到旧审批"；
 * - 安装即 skipWaiting，激活即 clients.claim，避免旧版本长期占用。
 */
const VERSION = "aieos-m-v2";
const SHELL = "/m";
const PAGES = `${VERSION}-pages`;
const STATIC = `${VERSION}-static`;

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches
      .open(PAGES)
      .then((cache) => cache.add(SHELL))
      .catch(() => {})
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    (async () => {
      const keys = await caches.keys();
      await Promise.all(
        keys.filter((k) => !k.startsWith(VERSION)).map((k) => caches.delete(k)),
      );
      await self.clients.claim();
    })(),
  );
});

function isStaticAsset(url) {
  if (url.pathname.startsWith("/_next/static/")) return true;
  return /\.(?:css|js|mjs|woff2?|ttf|otf|svg|png|jpe?g|webp|ico|json)$/i.test(url.pathname);
}

async function networkFirst(request) {
  const cache = await caches.open(PAGES);
  try {
    const res = await fetch(request);
    if (res && res.ok && new URL(request.url).pathname === SHELL) {
      cache.put(SHELL, res.clone());
    }
    return res;
  } catch {
    const cached = await cache.match(SHELL);
    if (cached) return cached;
    return new Response("离线：请连接网络后重试。", {
      status: 503,
      headers: { "Content-Type": "text/plain; charset=utf-8" },
    });
  }
}

async function staleWhileRevalidate(request) {
  const cache = await caches.open(STATIC);
  const cached = await cache.match(request);
  const network = fetch(request)
    .then((res) => {
      if (res && res.ok) cache.put(request, res.clone());
      return res;
    })
    .catch(() => null);
  if (cached) return cached;
  return (await network) || Response.error();
}

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  // 只接管同源页面/静态资源；后端在另一个端口（不同 origin），一律放行不缓存。
  if (url.origin !== self.location.origin) return;
  if (url.pathname.startsWith("/api")) return;

  if (req.mode === "navigate") {
    event.respondWith(networkFirst(req));
    return;
  }
  if (isStaticAsset(url)) {
    event.respondWith(staleWhileRevalidate(req));
  }
});

/* ---------- Web Push ---------- */

function pushPayload(event) {
  if (!event.data) return {};
  try {
    return event.data.json() || {};
  } catch {
    return { body: event.data.text() };
  }
}

/** 只允许跳到同源页面；外部/畸形地址统一回 /m，避免被 payload 带去任意站点。 */
function safeUrl(raw) {
  try {
    const url = new URL(typeof raw === "string" ? raw : "/m", self.location.origin);
    return url.origin === self.location.origin
      ? url.href
      : `${self.location.origin}/m`;
  } catch {
    return `${self.location.origin}/m`;
  }
}

self.addEventListener("push", (event) => {
  const payload = pushPayload(event);
  const title =
    typeof payload.title === "string" && payload.title
      ? payload.title
      : "AI-EOS";
  const body = typeof payload.body === "string" ? payload.body : "";
  const url = safeUrl(payload.url);
  event.waitUntil(
    self.registration.showNotification(title, {
      body,
      icon: "/icon.svg",
      tag: "aieos-notification",
      data: { url },
    }),
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const target = safeUrl(event.notification.data && event.notification.data.url);
  event.waitUntil(
    (async () => {
      const windows = await self.clients.matchAll({
        type: "window",
        includeUncontrolled: true,
      });
      for (const client of windows) {
        if ("focus" in client) {
          await client.focus();
          if ("navigate" in client && client.url !== target) {
            try {
              await client.navigate(target);
            } catch {
              /* 导航失败时保持当前页面 */
            }
          }
          return;
        }
      }
      if (self.clients.openWindow) await self.clients.openWindow(target);
    })(),
  );
});
