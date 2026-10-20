// Keeps the Render free instance of the downloader bot awake. Render sleeps a free
// service after 15 idle minutes; */13 fires at :00 :13 :26 :39 :52, so no gap reaches 15.
const TARGET = "https://everything-downloader.onrender.com/health";

export default {
  async scheduled(_event, _env, ctx) {
    ctx.waitUntil(fetch(TARGET, { headers: { "user-agent": "downloader-keepalive" } }));
  },
  async fetch() {
    return new Response("downloader keepalive", { headers: { "content-type": "text/plain" } });
  },
};
