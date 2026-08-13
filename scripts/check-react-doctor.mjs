import console from "node:console";
import { readFileSync } from "node:fs";
import process from "node:process";

const reportPath = process.argv[2];
if (reportPath === undefined) {
  console.error("A React Doctor JSON report is required.");
  process.exit(1);
}

const report = JSON.parse(readFileSync(reportPath, "utf8"));
const projects = Array.isArray(report) ? report : (report.projects ?? [report]);
const diagnostics = projects.flatMap(
  (project) => project.diagnostics ?? project.issues ?? [],
);
const scores = projects
  .map((project) =>
    typeof project.score === "object" ? project.score?.score : project.score,
  )
  .filter((score) => score !== undefined);

if (
  diagnostics.length !== 0 ||
  scores.length === 0 ||
  scores.some((score) => score !== 100)
) {
  console.error(
    `React Doctor scores ${JSON.stringify(scores)} with ${diagnostics.length} diagnostics.`,
  );
  process.exit(1);
}

console.log("React Doctor has zero diagnostics (score 100).");
