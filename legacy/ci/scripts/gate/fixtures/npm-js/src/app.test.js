import { expect, test } from "vitest";
import request from "supertest";
import { app } from "./app.js";

test("health returns the application result", async () => {
  const response = await request(app).get("/health");
  expect(response.status).toBe(200);
  expect(response.body).toEqual({ status: "ready" });
});
