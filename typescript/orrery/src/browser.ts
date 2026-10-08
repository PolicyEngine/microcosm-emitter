import { parseGraphDocument } from "@axiom-foundation/orrery";
import {
  inventory,
  publication,
  publicationId,
  MAX_FILE_BYTES,
} from "./contract.js";

export type Fetcher = (
  input: string | URL | Request,
  init?: RequestInit,
) => Promise<Response>;

async function boundedBytes(
  response: Response,
  maximum: number,
): Promise<Uint8Array> {
  if (!response.body) throw new Error("The stored graph is unavailable.");
  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > maximum)
        throw new Error("Response exceeds its publication size limit.");
      chunks.push(value);
    }
  } finally {
    await reader.cancel();
  }
  const result = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) {
    result.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return result;
}
async function digest(bytes: Uint8Array): Promise<string> {
  return Array.from(
    new Uint8Array(
      await crypto.subtle.digest("SHA-256", bytes as Uint8Array<ArrayBuffer>),
    ),
    (value) => value.toString(16).padStart(2, "0"),
  ).join("");
}
export async function loadDocument(
  id: string,
  signal: AbortSignal,
  fetcher: Fetcher = fetch,
) {
  publicationId(id);
  const response = await fetcher(`/api/runs/${encodeURIComponent(id)}/`, {
    signal,
    redirect: "error",
    credentials: "omit",
  });
  if (response.status === 404)
    throw new Error("This graph publication was not found.");
  if (!response.ok)
    throw new Error("Graph publication metadata is unavailable.");
  const bytes = await boundedBytes(response, 256 * 1024);
  const saved = publication(
    JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes)),
    id,
  );
  if (
    (await digest(
      new TextEncoder().encode(JSON.stringify(inventory(saved))),
    )) !== saved.inventory_sha256
  )
    throw new Error("Inventory digest does not match its publication.");
  const file = saved.files.find((file) => file.role === "graph")!;
  const graph = await fetcher(file.url, {
    signal,
    redirect: "error",
    credentials: "omit",
  });
  if (!graph.ok) throw new Error("The stored graph is unavailable.");
  const payload = await boundedBytes(
    graph,
    Math.min(file.bytes, MAX_FILE_BYTES),
  );
  if (payload.length !== file.bytes)
    throw new Error("Graph size does not match its publication.");
  const checksum = await digest(payload);
  if (checksum !== file.sha256)
    throw new Error("Graph digest does not match its publication.");
  return {
    document: parseGraphDocument(
      JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(payload)),
    ),
    digest: checksum,
  };
}
