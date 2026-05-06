import { client } from "./client";
import type { DocumentMeta } from "../types";

export const Documents = {
  list: () => client.get<DocumentMeta[]>("/documents").then((r) => r.data),
  upload: (file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    return client.post<DocumentMeta>("/documents", fd).then((r) => r.data);
  },
  remove: (id: string) => client.delete(`/documents/${id}`).then(() => undefined),
  wipeIndex: () => client.delete("/admin/index").then((r) => r.data),
  wipeBlobs: (prefix?: string) =>
    client
      .delete("/admin/blobs", { params: prefix !== undefined ? { prefix } : {} })
      .then((r) => r.data),
  wipeAll: () => client.delete("/admin/all").then((r) => r.data),
};

