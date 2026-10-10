import express from "express";

export function message() {
  return "ready";
}

export const app = express();
app.get("/health", (_request, response) => response.json({ status: message() }));
