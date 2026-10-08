import { createHash } from "node:crypto";
import {
  ApiError,
  inventory,
  publication,
  publicationId,
  type Inventory,
} from "./contract.js";

export function sha256(bytes: string | Uint8Array): string {
  return createHash("sha256").update(bytes).digest("hex");
}
export function inventoryDigest(value: Inventory): string {
  return sha256(JSON.stringify(inventory(value)));
}
export function publicationPath(id: string): string {
  return `publications/${publicationId(id)}.json`;
}
export function objectPath(value: Inventory, name: string): string {
  return `objects/${value.publication_id}/${inventoryDigest(value)}/${name}`;
}
export interface PublicationStorage {
  read(
    path: string,
    maximum: number,
  ): Promise<{ bytes: Uint8Array; url: string } | null>;
}
export function createPublicationReader(storage: PublicationStorage) {
  async function read(id: string) {
    const object = await storage.read(publicationPath(id), 256 * 1024);
    if (!object) return null;
    if (object.bytes.byteLength > 256 * 1024)
      throw new ApiError(400, "metadata_too_large");
    const value = publication(
      JSON.parse(
        new TextDecoder("utf-8", { fatal: true }).decode(object.bytes),
      ),
      id,
    );
    if (inventoryDigest(value) !== value.inventory_sha256)
      throw new ApiError(400, "inventory_digest_mismatch");
    return value;
  }
  return {
    read,
    async evidence(id: string, name: string) {
      return (
        (await read(id))?.files.find((file) => file.name === name)?.url ?? null
      );
    },
  };
}
