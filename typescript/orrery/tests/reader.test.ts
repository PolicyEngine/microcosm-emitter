import { expect, test } from "bun:test";
import { loadDocument, type Fetcher } from "../src/browser.js";
import {
  createPublicationReader,
  inventoryDigest,
  sha256,
} from "../src/server.js";
import { inventory, type Publication } from "../src/contract.js";

const document = {
  schemaVersion: "graph-explorer/v1",
  id: "fixture",
  title: "Fixture",
  nodes: [],
  edges: [],
};
function fixture(payload = JSON.stringify(document)): Publication {
  const value = inventory({
    version: 1,
    publication_id: "graph",
    files: [
      {
        name: "graph.orrery.json",
        role: "graph",
        bytes: new TextEncoder().encode(payload).length,
        sha256: sha256(payload),
      },
    ],
  });
  return {
    ...value,
    inventory_sha256: inventoryDigest(value),
    published_at: "2026-10-08T00:00:00Z",
    files: value.files.map((file) => ({
      ...file,
      url: "https://example.public.blob.vercel-storage.com/graph.json",
    })),
  };
}
function fetcher(saved: unknown, payload = JSON.stringify(document)): Fetcher {
  let calls = 0;
  return async (_url, init) => {
    expect(init?.credentials).toBe("omit");
    expect(init?.redirect).toBe("error");
    return ++calls === 1 ? Response.json(saved) : new Response(payload);
  };
}
const signal = () => new AbortController().signal;
test("browser verifies metadata, exact bytes and graph parser", async () => {
  const result = await loadDocument("graph", signal(), fetcher(fixture()));
  expect(result.document.id).toBe("fixture");
  expect(result.digest).toBe(sha256(JSON.stringify(document)));
});
test("missing publications return an explicit error", async () => {
  await expect(
    loadDocument(
      "graph",
      signal(),
      async () => new Response("", { status: 404 }),
    ),
  ).rejects.toThrow("not found");
});
for (const field of [
  "publication_id",
  "inventory_sha256",
  "published_at",
] as const) {
  test("rejects invalid " + field, async () => {
    const saved = { ...fixture(), [field]: "invalid" };
    await expect(
      loadDocument("graph", signal(), fetcher(saved)),
    ).rejects.toThrow();
  });
}
for (const url of [
  "http://example.public.blob.vercel-storage.com/x",
  "https://example.com/x",
  "https://user:secret@example.public.blob.vercel-storage.com/x",
]) {
  test("rejects credential-bearing or non-storage URL " + url, async () => {
    const saved = fixture();
    saved.files[0].url = url;
    await expect(
      loadDocument("graph", signal(), fetcher(saved)),
    ).rejects.toThrow();
  });
}
test("rejects modified graph bytes", async () => {
  await expect(
    loadDocument(
      "graph",
      signal(),
      fetcher(fixture(), "x".repeat(JSON.stringify(document).length)),
    ),
  ).rejects.toThrow("digest");
});
test("bounds graph streaming before parsing", async () => {
  await expect(
    loadDocument(
      "graph",
      signal(),
      fetcher(fixture(), JSON.stringify(document) + "extra"),
    ),
  ).rejects.toThrow("size limit");
});
test("bounds metadata streaming", async () => {
  await expect(
    loadDocument(
      "graph",
      signal(),
      async () => new Response("x".repeat(256 * 1024 + 1)),
    ),
  ).rejects.toThrow("size limit");
});
test("rejects a valid but incorrect inventory digest", async () => {
  await expect(
    loadDocument(
      "graph",
      signal(),
      fetcher({ ...fixture(), inventory_sha256: "a".repeat(64) }),
    ),
  ).rejects.toThrow("Inventory digest");
});
test("server reads only completed publications and listed evidence", async () => {
  const saved = fixture();
  const reads: string[] = [];
  const reader = createPublicationReader({
    async read(path, maximum) {
      reads.push(path);
      expect(maximum).toBe(256 * 1024);
      return path === "publications/graph.json"
        ? {
            bytes: new TextEncoder().encode(JSON.stringify(saved)),
            url: "unused",
          }
        : null;
    },
  });
  expect(await reader.read("missing")).toBeNull();
  expect((await reader.read("graph"))?.publication_id).toBe("graph");
  expect(await reader.evidence("graph", "private.h5")).toBeNull();
  expect(await reader.evidence("graph", "graph.orrery.json")).toBe(
    saved.files[0].url,
  );
  expect(reads.every((path) => path.startsWith("publications/"))).toBe(true);
});
test("server rejects corrupt stored metadata", async () => {
  const saved = { ...fixture(), inventory_sha256: "a".repeat(64) };
  const reader = createPublicationReader({
    async read() {
      return {
        bytes: new TextEncoder().encode(JSON.stringify(saved)),
        url: "unused",
      };
    },
  });
  await expect(reader.read("graph")).rejects.toThrow(
    "inventory_digest_mismatch",
  );
});
