/**
 * VoiceGetter on Cloudflare.
 *
 * - The Worker serves the phone web page and checks the password.
 * - A Cloudflare Container runs the Python app (../Dockerfile) that does the audio work.
 * - The container stays on while a job is queued or running. When it has been idle for
 *   `sleepAfter`, finished results are copied to R2 and the container stops (no cost while stopped).
 * - Results in R2 are listed and downloaded without starting the container.
 */
import { Container, getContainer } from "@cloudflare/containers";
import INDEX_HTML from "../../app/static/index.html";

export interface Env {
  VOICEGETTER: DurableObjectNamespace<VoiceGetterContainer>;
  RESULTS: R2Bucket;
  PASSWORD?: string;
  KEEP_DAYS?: string;
}

type Job = {
  id: string;
  state: "queued" | "running" | "done" | "error" | "cancelled";
  created: number;
  has_zip?: boolean;
  log?: string[];
  [k: string]: unknown;
};

const PORT = 8000;
const INSTANCE = "main"; // One container: jobs run one at a time.
const COOKIES_KEY = "settings/cookies.txt";
const jobKey = (id: string, file: string) => `jobs/${id}/${file}`;

// ---------------------------------------------------------------------------
// Container (Durable Object)
// ---------------------------------------------------------------------------

export class VoiceGetterContainer extends Container<Env> {
  defaultPort = PORT;
  sleepAfter = "10m";
  enableInternet = true;

  envVars = {
    VOICEGETTER_PASSWORD: this.env.PASSWORD ?? "",
    VOICEGETTER_DATA: "/data",
    VOICEGETTER_KEEP_HOURS: "24",
  };

  /** Read from the container WITHOUT renewing the sleep timer. Returns null when it is not running. */
  async peek(path: string): Promise<{ status: number; body: string } | null> {
    if (!this.ctx.container?.running) return null;
    try {
      const res = await this.ctx.container
        .getTcpPort(PORT)
        .fetch(`http://container${path}`, { headers: { "x-password": this.env.PASSWORD ?? "" } });
      return { status: res.status, body: await res.text() };
    } catch {
      return null;
    }
  }

  /** Start the container (if needed) and wait until the web server answers. */
  async ensureStarted(): Promise<void> {
    await this.startAndWaitForPorts({
      ports: PORT,
      cancellationOptions: { instanceGetTimeoutMS: 120_000, portReadyTimeoutMS: 180_000, waitInterval: 1_000 },
    });
  }

  /** Called when nobody used the container for `sleepAfter`. */
  override async onActivityExpired(): Promise<void> {
    const res = await this.peek("/api/jobs");
    if (!res || res.status !== 200) {
      await this.stop();
      return;
    }
    const jobs = JSON.parse(res.body) as Job[];
    if (jobs.some((j) => j.state === "queued" || j.state === "running")) {
      console.log("Job in progress: keeping the container on");
      return; // Not stopping renews the timer; this hook runs again later.
    }
    for (const job of jobs) {
      try {
        await this.archive(job);
      } catch (e) {
        console.error(`Could not save job ${job.id} to R2`, e);
      }
    }
    console.log("Idle: stopping the container");
    await this.stop();
  }

  /** Copy a finished job (meta + files) from the container to R2. */
  private async archive(job: Job): Promise<void> {
    if (job.state !== "done" && job.state !== "error") return;
    if (await this.env.RESULTS.head(jobKey(job.id, "meta.json"))) return;
    if (job.state === "done") {
      for (const kind of job.has_zip ? ["mp3", "zip"] : ["mp3"]) {
        const res = await this.ctx.container!.getTcpPort(PORT).fetch(
          `http://container/api/jobs/${job.id}/${kind}`,
          { headers: { "x-password": this.env.PASSWORD ?? "" } },
        );
        const len = Number(res.headers.get("content-length"));
        if (!res.ok || !res.body || !len) throw new Error(`download ${kind}: HTTP ${res.status}`);
        const { readable, writable } = new FixedLengthStream(len);
        const pipe = res.body.pipeTo(writable);
        await this.env.RESULTS.put(jobKey(job.id, kind), readable, {
          httpMetadata: {
            contentType: res.headers.get("content-type") ?? "application/octet-stream",
            contentDisposition: res.headers.get("content-disposition") ?? undefined,
          },
        });
        await pipe;
      }
    }
    const meta = { ...job, log: (job.log ?? []).slice(-60), archived: true };
    await this.env.RESULTS.put(jobKey(job.id, "meta.json"), JSON.stringify(meta), {
      httpMetadata: { contentType: "application/json" },
    });
    console.log(`Saved job ${job.id} to R2`);
  }
}

// ---------------------------------------------------------------------------
// Worker
// ---------------------------------------------------------------------------

const json = (data: unknown, status = 200) =>
  new Response(JSON.stringify(data), {
    status,
    headers: { "content-type": "application/json", "cache-control": "no-store" },
  });

function authorized(req: Request, url: URL, env: Env): boolean {
  const given = req.headers.get("x-password") ?? url.searchParams.get("key") ?? "";
  if (!env.PASSWORD || given.length !== env.PASSWORD.length) return false;
  let diff = 0; // constant-time compare
  for (let i = 0; i < given.length; i++) diff |= given.charCodeAt(i) ^ env.PASSWORD.charCodeAt(i);
  return diff === 0;
}

async function archivedJobs(env: Env, ctx: ExecutionContext): Promise<Job[]> {
  const keepMs = Number(env.KEEP_DAYS ?? "14") * 86_400_000;
  const listed = await env.RESULTS.list({ prefix: "jobs/", limit: 1000 });
  const metas = listed.objects.filter((o) => o.key.endsWith("/meta.json"));
  const jobs: Job[] = [];
  const expired: string[] = [];
  await Promise.all(
    metas.map(async (o) => {
      const obj = await env.RESULTS.get(o.key);
      if (!obj) return;
      const job = (await obj.json()) as Job;
      if (Date.now() - job.created * 1000 > keepMs) expired.push(job.id);
      else jobs.push(job);
    }),
  );
  if (expired.length) ctx.waitUntil(Promise.all(expired.map((id) => deleteArchived(env, id))));
  return jobs;
}

async function deleteArchived(env: Env, id: string): Promise<boolean> {
  const keys = ["meta.json", "mp3", "zip"].map((f) => jobKey(id, f));
  const found = await env.RESULTS.head(keys[0]);
  await env.RESULTS.delete(keys);
  return !!found;
}

/** Serve an R2 object, with Range support (iPhone Safari needs it to play audio). */
async function serveFile(req: Request, url: URL, env: Env, key: string): Promise<Response | null> {
  const head = await env.RESULTS.head(key);
  if (!head) return null;
  const range = req.headers.has("range") ? req.headers : undefined;
  const obj = await env.RESULTS.get(key, range ? { range } : {});
  if (!obj || !("body" in obj)) return null;
  const headers = new Headers();
  obj.writeHttpMetadata(headers);
  headers.set("accept-ranges", "bytes");
  headers.set("etag", obj.httpEtag);
  if (url.searchParams.get("play") === "1") {
    headers.set("content-disposition", (headers.get("content-disposition") ?? "").replace(/^attachment/, "inline"));
  }
  const r = obj.range as { offset?: number; length?: number } | undefined;
  if (range && r && (r.offset !== undefined || r.length !== undefined)) {
    const start = r.offset ?? head.size - (r.length ?? 0);
    const len = r.length ?? head.size - start;
    headers.set("content-range", `bytes ${start}-${start + len - 1}/${head.size}`);
    headers.set("content-length", String(len));
    return new Response(obj.body, { status: 206, headers });
  }
  headers.set("content-length", String(head.size));
  return new Response(obj.body, { headers });
}

export default {
  async fetch(req: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    const url = new URL(req.url);
    const path = url.pathname;

    if (req.method === "GET" && (path === "/" || path === "/index.html")) {
      return new Response(INDEX_HTML, {
        headers: { "content-type": "text/html; charset=utf-8", "cache-control": "no-store" },
      });
    }
    if (path === "/api/config") return json({ password_required: true });
    if (!path.startsWith("/api/")) return new Response("Not found", { status: 404 });
    if (!env.PASSWORD) {
      return json({ error: "Setup is not finished: run `npx wrangler secret put PASSWORD`." }, 500);
    }
    if (!authorized(req, url, env)) return json({ error: "Wrong password" }, 401);

    const box = getContainer(env.VOICEGETTER, INSTANCE);
    // Status reads must not fail when the container is broken: saved results in R2 still work.
    const peek = (p: string) => box.peek(p).catch((e: unknown) => (console.error("peek failed", e), null));

    // Job list: archived jobs from R2 + live jobs (read without waking or keeping the container on).
    if (path === "/api/jobs" && req.method === "GET") {
      const [archived, live] = await Promise.all([archivedJobs(env, ctx), peek("/api/jobs")]);
      const byId = new Map<string, Job>(archived.map((j) => [j.id, j]));
      if (live && live.status === 200) for (const j of JSON.parse(live.body) as Job[]) byId.set(j.id, j);
      return json([...byId.values()].sort((a, b) => b.created - a.created));
    }

    // New job: start the container, give it the saved cookies, then pass the request on.
    if (path === "/api/jobs" && req.method === "POST") {
      try {
        await box.ensureStarted();
      } catch (e) {
        return json({ error: `The server could not start. Try again in a minute. (${String(e)})` }, 503);
      }
      const cookies = await env.RESULTS.get(COOKIES_KEY);
      if (cookies) {
        await box.fetch(new Request(`${url.origin}/api/cookies`, {
          method: "POST",
          headers: { "x-password": env.PASSWORD, "content-type": "text/plain" },
          body: await cookies.text(),
        }));
      }
      return box.fetch(req);
    }

    // Cookies: kept in R2 so they survive container restarts.
    if (path === "/api/cookies" && req.method === "POST") {
      const text = await req.text();
      if (text.trim() && !text.includes("\t")) {
        return json({ error: "This does not look like a cookies.txt file (Netscape format)." }, 400);
      }
      if (text.trim()) await env.RESULTS.put(COOKIES_KEY, text);
      else await env.RESULTS.delete(COOKIES_KEY);
      if (await peek("/api/config")) {
        await box.fetch(new Request(req.url, {
          method: "POST",
          headers: { "x-password": env.PASSWORD, "content-type": "text/plain" },
          body: text,
        }));
      }
      return json({ ok: true, cookies: !!text.trim() });
    }

    const m = path.match(/^\/api\/jobs\/([a-f0-9]{6,32})(?:\/(mp3|zip|cancel))?$/);
    if (m) {
      const [, id, sub] = m;
      const running = !!(await peek("/api/config"));

      if ((sub === "mp3" || sub === "zip") && req.method === "GET") {
        const fromR2 = await serveFile(req, url, env, jobKey(id, sub));
        if (fromR2) return fromR2;
        if (running) return box.fetch(req);
        return json({ error: "File not found. Results are kept for " + (env.KEEP_DAYS ?? "14") + " days." }, 404);
      }
      if (sub === "cancel" && req.method === "POST") {
        return running ? box.fetch(req) : json({ error: "This job is not running." }, 409);
      }
      if (!sub && req.method === "DELETE") {
        if (running) {
          const live = await peek(`/api/jobs/${id}`);
          if (live && live.status === 200) {
            const res = await box.fetch(req);
            if (!res.ok) return res;
          }
        }
        await deleteArchived(env, id);
        return json({ ok: true });
      }
      if (!sub && req.method === "GET") {
        const live = running ? await peek(`/api/jobs/${id}`) : null;
        if (live && live.status === 200) return new Response(live.body, { headers: { "content-type": "application/json" } });
        const meta = await env.RESULTS.get(jobKey(id, "meta.json"));
        return meta ? new Response(meta.body, { headers: { "content-type": "application/json" } }) : json({ error: "Job not found" }, 404);
      }
    }
    return json({ error: "Not found" }, 404);
  },
} satisfies ExportedHandler<Env>;
