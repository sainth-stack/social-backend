// pm2 — OpsBrain Social Media production
//
//   pm2 start scripts/pm2.ecosystem.config.cjs
//
// Local dev: ./scripts/ecosystem.sh up

const fs = require("fs");
const path = require("path");

const BACKEND = process.env.SOCIAL_MEDIA_ROOT || path.resolve(__dirname, "..");
const REPO_ROOT = path.resolve(BACKEND, "..");
const FRONTEND = path.join(REPO_ROOT, "frontend");

function venvPython() {
  for (const name of [".venv", "venv"]) {
    const py = path.join(BACKEND, name, "bin", "python");
    if (fs.existsSync(py)) return py;
  }
  return "python3";
}

const PYTHON = venvPython();
const PORT = process.env.PORT || "8000";
const FRONTEND_PORT = process.env.FRONTEND_PORT || "3001";
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

const nextApp = (name, root, port) => ({
  ...base,
  name,
  cwd: root,
  script: path.join(root, "node_modules/next/dist/bin/next"),
  args: `start -p ${port}`,
  interpreter: "none",
  env: { NODE_ENV: "production" },
  out_file: path.join(LOGS, `${name}.log`),
  error_file: path.join(LOGS, `${name}.log`),
});

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
