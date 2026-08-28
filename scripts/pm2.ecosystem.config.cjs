// PM2 production — OpsBrain Social Media
//
// API :5000 | Frontend :5001 | Auto-detects ../social-frontend or ../frontend
//
// Start (from backend/):
//   pm2 start scripts/pm2.ecosystem.config.cjs
//   pm2 save
//
// Stop / remove:
//   ./scripts/pm2-stop.sh
//
// Restart:
//   ./scripts/pm2-stop.sh && pm2 start scripts/pm2.ecosystem.config.cjs && pm2 save
//
// Local dev (no PM2): ./scripts/ecosystem.sh up

const fs = require("fs");
const path = require("path");

const BACKEND = process.env.SOCIAL_MEDIA_ROOT || path.resolve(__dirname, "..");
const REPO_ROOT = path.resolve(BACKEND, "..");

function resolveFrontendRoot() {
  if (process.env.SOCIAL_MEDIA_FRONTEND) {
    return path.resolve(process.env.SOCIAL_MEDIA_FRONTEND);
  }
  const candidates = [
    path.join(REPO_ROOT, "social-frontend"),
    path.join(REPO_ROOT, "frontend"),
  ];
  for (const dir of candidates) {
    if (fs.existsSync(dir)) return dir;
  }
  return candidates[0];
}

const FRONTEND = resolveFrontendRoot();

function venvPython() {
  for (const name of [".venv", "venv"]) {
    const py = path.join(BACKEND, name, "bin", "python");
    if (fs.existsSync(py)) return py;
  }
  return "python3";
}

const PYTHON = venvPython();
const PORT = process.env.PORT || "5000";
const FRONTEND_PORT = process.env.FRONTEND_PORT || "5001";
const API_WORKERS = process.env.API_WORKERS || "2";
const WORKER_CONCURRENCY = process.env.CELERY_CONCURRENCY || "4";
const LOGS = path.join(BACKEND, ".run");

const QUEUES = [
  "social_publish",
  "social_analytics",
  "social_maintenance",
].join(",");

const base = {
  autorestart: true,
  max_restarts: 15,
  min_uptime: "10s",
  kill_timeout: 10000,
  merge_logs: true,
  time: true,
};

const pythonApp = (name, args, log) => ({
  ...base,
  name,
  cwd: BACKEND,
  script: PYTHON,
  args,
  interpreter: "none",
  out_file: path.join(LOGS, `${log}.log`),
  error_file: path.join(LOGS, `${log}.log`),
});

const nextApp = (name, root, port) => {
  const script = path.join(root, "node_modules/next/dist/bin/next");
  if (!fs.existsSync(script)) {
    throw new Error(
      `Next.js not found at ${script}. Run: cd ${root} && npm ci && npm run build`,
    );
  }
  return {
    ...base,
    name,
    cwd: root,
    script,
    args: `start -p ${port}`,
    interpreter: "none",
    env: { NODE_ENV: "production" },
    out_file: path.join(LOGS, `${name}.log`),
    error_file: path.join(LOGS, `${name}.log`),
  };
};

module.exports = {
  apps: [
    pythonApp(
      "social-media-api",
      `-m uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --workers ${API_WORKERS}`,
      "api",
    ),
    pythonApp(
      "social-media-worker",
      `-m celery -A workers.celery_app:celery_app worker -l info -Q ${QUEUES} -P prefork -c ${WORKER_CONCURRENCY} -n worker@%h --without-heartbeat --without-gossip --without-mingle`,
      "worker",
    ),
    pythonApp(
      "social-media-beat",
      "-m celery -A workers.celery_app:celery_app beat -l info",
      "beat",
    ),
    nextApp("social-media-frontend", FRONTEND, FRONTEND_PORT),
  ],
};
