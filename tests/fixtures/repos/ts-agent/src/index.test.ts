import { describe, expect, it } from "vitest";

import { charge } from "./billing";

describe("charge", () => {
  it("rejects an empty customer", async () => {
    await expect(charge("", 0)).rejects.toThrow();
  });
});
