import type { AcquisitionList, BatchImportResult, Book, BookFields, Bootstrap, BulkAction, BulkResult, ImportPreview, ListDetail, PreviewRow, Settings, Template, UpdateInfo } from "./types";

type Fetcher = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;
export function createLibraryApi(fetcher: Fetcher = (input, init) => fetch(input, init)) {
  let token = "";
  async function response(path: string, init: RequestInit = {}): Promise<Response> {
    const headers = new Headers(init.headers);
    if (token) headers.set("X-Suseoro-Token", token);
    if (init.body && !(init.body instanceof FormData)) headers.set("Content-Type", "application/json");
    let result: Response;
    try { result = await fetcher(`/api/library${path}`, { ...init, headers, credentials: "same-origin" }); }
    catch { throw new Error("수서로에 연결할 수 없습니다. 프로그램이 열려 있는지 확인해 주세요."); }
    if (!result.ok) {
      let message = `요청을 처리하지 못했습니다 (${result.status}).`;
      try {
        const body: unknown = await result.json();
        if (body && typeof body === "object" && "detail" in body && typeof body.detail === "string") message = body.detail;
      } catch { /* The server may return a plain-text failure. */ }
      throw new Error(message);
    }
    return result;
  }
  async function json<T>(path: string, init?: RequestInit): Promise<T> { return (await response(path, init)).json() as Promise<T>; }
  const send = (body: unknown, method = "POST"): RequestInit => ({ method, body: JSON.stringify(body) });
  async function upload<T>(path: string, file: File): Promise<T> { const data = new FormData(); data.append("file", file); return json<T>(path, { method: "POST", body: data }); }
  async function download(path: string, fallback: string, body?: unknown) {
    const result = await response(path, body === undefined ? {} : send(body));
    const disposition = result.headers.get("Content-Disposition") ?? "";
    const encoded = /filename\*=UTF-8''([^;]+)/i.exec(disposition)?.[1];
    const plain = /filename="?([^";]+)"?/i.exec(disposition)?.[1];
    let filename = plain ?? fallback;
    if (encoded) { try { filename = decodeURIComponent(encoded); } catch { filename = fallback; } }
    filename = filename.replace(/[\\/]/g, "_");
    const url = URL.createObjectURL(await result.blob());
    const link = document.createElement("a"); link.href = url; link.download = filename;
    document.body.append(link); link.click(); link.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 30_000);
  }
  return {
    async bootstrap() { const data = await json<Bootstrap>("/bootstrap"); token = data.csrf_token; return data; },
    list: (id: string) => json<ListDetail>(`/lists/${id}`),
    createList: (data: Pick<AcquisitionList, "name" | "year" | "budget" | "discount_percent">) => json<AcquisitionList>("/lists", send(data)),
    updateList: (id: string, data: Partial<AcquisitionList>) => json<AcquisitionList>(`/lists/${id}`, send(data, "PATCH")),
    addBook: (id: string, book: BookFields) => json<Book>(`/lists/${id}/books`, send(book)),
    updateBook: (id: string, bookId: string, book: Partial<BookFields>) => json<Book>(`/lists/${id}/books/${bookId}`, send(book, "PATCH")),
    deleteBook: (id: string, bookId: string) => json<{ ok: boolean }>(`/lists/${id}/books/${bookId}`, { method: "DELETE" }),
    restoreBook: (id: string, bookId: string) => json<Book>(`/lists/${id}/books/${bookId}/restore`, { method: "POST" }),
    lookup: (isbn: string) => json<{ found: boolean; book?: BookFields; warnings: string[] }>("/lookup", send({ isbn })),
    preview: (file: File) => upload<ImportPreview>("/imports/preview", file),
    remapPreview: (id: string, mapping: Record<string, string>) => json<ImportPreview>(`/imports/${id}/preview`, send({ mapping })),
    importRows: (id: string, import_id: string, rows: PreviewRow[], kind: "recommendations" | "holdings") => json<{ added: number; warnings: string[] }>(`/lists/${id}/imports`, send({ import_id, rows, kind })),
    importBatch: (id: string, imports: { import_id: string; rows: PreviewRow[] }[], deduplicate: boolean, request_id: string) => json<BatchImportResult>(`/lists/${id}/imports/batch`, send({ imports, deduplicate, request_id })),
    bulkBooks: (id: string, book_ids: string[], action: BulkAction) => json<BulkResult>(`/lists/${id}/books/bulk`, send({ book_ids, action })),
    undoOperation: (id: string, operationId: string) => json<{ restored: number }>(`/lists/${id}/operations/${operationId}/undo`, { method: "POST" }),
    templates: () => json<Template[]>("/templates"),
    uploadTemplate: (file: File) => upload<Template>("/templates", file),
    exportList: (id: string, format: "xlsx" | "csv" | "html", template_id?: string) => download(`/lists/${id}/export`, `발주서.${format}`, { format, ...(template_id ? { template_id } : {}) }),
    settings: (settings: { school_name?: string; nl_api_key?: string; text_size?: number; row_density?: "comfortable" | "compact" }) => json<Settings>("/settings", send(settings, "PATCH")),
    backup: () => download("/backup", "수서로-백업.json"),
    restore: (file: File) => upload<{ ok: boolean }>("/restore", file),
    updates: () => json<UpdateInfo>("/updates"),
    updateStatus: () => json<UpdateInfo>("/updates/status"),
    updatePreferences: (auto_enabled: boolean) => json<UpdateInfo>("/updates/preferences", send({ auto_enabled }, "PATCH")),
    installUpdate: () => json<{ started: boolean }>("/updates/install", { method: "POST" }),
    shutdown: () => json<{ ok: boolean }>("/shutdown", { method: "POST" }),
  };
}
export type LibraryApi = ReturnType<typeof createLibraryApi>;
