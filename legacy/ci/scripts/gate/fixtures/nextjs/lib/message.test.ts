import { expect, test } from "vitest";
import { GET } from "../app/health/route";
test("health route returns the application result", async () => {
  const response = GET();
  expect(response.status).toBe(200);
  expect(await response.json()).toEqual({ status: "ready" });
});
