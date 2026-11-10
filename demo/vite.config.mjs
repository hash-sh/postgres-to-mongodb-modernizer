// Runs the ORIGINAL, unmodified Conduit React frontend (vendor/original/frontend) against either backend.
// The only thing that changes is where /api is proxied to - that is the point: same UI, same API contract.
//   BACKEND=http://localhost:3001  -> original app (Sequelize + Postgres)
//   BACKEND=http://localhost:3002  -> modernized app (Mongoose + MongoDB)
import react from "../vendor/original/node_modules/@vitejs/plugin-react-swc/index.js";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));

export default {
  root: path.resolve(here, "../vendor/original/frontend"),
  plugins: [react()],
  server: {
    port: Number(process.env.PORT || 3100),
    strictPort: true,
    proxy: { "/api": { target: process.env.BACKEND || "http://localhost:3002" } },
  },
};
