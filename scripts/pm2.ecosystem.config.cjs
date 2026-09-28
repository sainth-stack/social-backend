// PM2 production config
// API :5000 | Frontend :5001
//
//   ./scripts/pm2-start.sh
//   ./scripts/pm2-stop.sh

const fs = require("fs");
const path = require("path");

const BACKEND = path.resolve(__dirname, "..");
const REPO_ROOT = path.resolve(BACKEND, "..");

function resolveFrontend() {
  for (const dir of [
    path.join(REPO_ROOT, "social-frontend"),
    path.join(REPO_ROOT, "frontend"),
  ]) {
    if (fs.existsSync(dir)) return dir;
  }
  return path.join(REPO_ROOT, "frontend");
}

function pythonBin() {
  for (const name of [".venv", "venv"]) {
    const py = path.join(BACKEND, name, "bin", "python");
    if (fs.existsSync(py)) return py;
  }
  return "python3";
}

const PYTHON = pythonBin();
const FRONTEND = resolveFrontend();
const LOGS = path.join(BACKEND, ".run");
const PORT = process.env.PORT || "5000";
const FRONTEND_PORT = process.env.FRONTEND_PORT || "5001";

const base = {
  cwd: BACKEND,
  interpreter: "none",
  autorestart: true,
  max_restarts: 15,
  min_uptime: "10s",
  merge_logs: true,
  time: true,
};

module.exports = {
  apps: [
    {
      ...base,
      name: "social-media-api",
      script: PYTHON,
      args: `-m uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --workers 2`,
      out_file: path.join(LOGS, "api.log"),
      error_file: path.join(LOGS, "api.log"),
    },
    {
      ...base,
      name: "social-media-worker",
      script: PYTHON,
      args: "-m celery -A workers.celery_app:celery_app worker -l info -Q social_publish,social_analytics,social_maintenance -P prefork -c 4 -n worker@%h",
      out_file: path.join(LOGS, "worker.log"),
      error_file: path.join(LOGS, "worker.log"),
    },
    {
      ...base,
      name: "social-media-beat",
      script: PYTHON,
      args: "-m celery -A workers.celery_app:celery_app beat -l info",
      out_file: path.join(LOGS, "beat.log"),
      error_file: path.join(LOGS, "beat.log"),
    },
    {
      ...base,
      name: "social-media-frontend",
      cwd: FRONTEND,
      script: path.join(FRONTEND, "node_modules/next/dist/bin/next"),
      args: `start -p ${FRONTEND_PORT}`,
      env: { NODE_ENV: "production" },
      out_file: path.join(LOGS, "frontend.log"),
      error_file: path.join(LOGS, "frontend.log"),
    },
  ],
};
