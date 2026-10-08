import { describe, expect, test } from "bun:test";
import { inventory, MAX_FILE_BYTES } from "../src/contract.js";
import { inventoryDigest, objectPath } from "../src/server.js";

const graph = {
  name: "graph.orrery.json",
  role: "graph",
  bytes: 100,
  sha256: "a".repeat(64),
};
const valid = { version: 1, publication_id: "uk-run-123", files: [graph] };
describe("publication inventory", () => {
  test("canonical order and content-addressed paths", () => {
    const evidence = {
      ...graph,
      name: "execution.evidence.json",
      role: "index",
    };
    const a = inventory({ ...valid, files: [graph, evidence] });
    const b = inventory({ ...valid, files: [evidence, graph] });
    expect(inventoryDigest(a)).toBe(inventoryDigest(b));
    expect(objectPath(a, graph.name)).toBe(
      `objects/uk-run-123/${inventoryDigest(a)}/graph.orrery.json`,
    );
  });
  for (const value of [
    { ...valid, version: 2 },
    { ...valid, publication_id: "../bad" },
    { ...valid, files: [graph, graph] },
    { ...valid, files: [{ ...graph, name: "../graph.orrery.json" }] },
    { ...valid, files: [{ ...graph, bytes: MAX_FILE_BYTES + 1 }] },
    { ...valid, files: [{ ...graph, bytes: -1 }] },
    { ...valid, files: [{ ...graph, sha256: "wrong" }] },
    { ...valid, files: [{ ...graph, name: "private.h5", role: "manifest" }] },
    {
      ...valid,
      files: [{ ...graph, name: "unreviewed.json", role: "manifest" }],
    },
    { ...valid, files: [] },
  ])
    test(`rejects ${JSON.stringify(value).slice(0, 100)}`, () =>
      expect(() => inventory(value)).toThrow());
});
