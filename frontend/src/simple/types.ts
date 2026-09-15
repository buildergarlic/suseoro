export interface AcquisitionList {
  id: string; name: string; year: number; budget: number; discount_percent: number; created_at: string;
}
export interface BookFields {
  title: string; author: string; publisher: string; isbn: string; price: number | null;
  quantity: number; selected: boolean; category: string; requester: string; audience: string;
  priority: "high" | "normal" | "low"; source: string; note: string; published_date: string;
  link: string; needs_review: boolean; warnings: string[];
}
export interface Book extends BookFields { id: string; held: boolean; duplicate: boolean }
export interface PreviewRow extends BookFields {
  raw_values?: Record<string, unknown>;
  raw?: string | Record<string, unknown>; raw_text?: string; provenance?: unknown;
  source_file?: string; source_sheet?: string; source_page?: number; source_row?: number;
}
export interface Summary {
  selected_count: number; total_quantity: number; list_total: number; order_total: number;
  remaining: number; missing_price_count: number; review_count: number; held_count: number; duplicate_count: number;
}
export interface ListDetail { list: AcquisitionList; books: Book[]; summary: Summary }
export interface Settings { school_name: string; nl_api_key_configured: boolean }
export interface UpdateInfo {
  current_version?: string; latest_version?: string; available: boolean; url?: string; message?: string;
}
export interface Bootstrap { version: string; csrf_token: string; settings: Settings; lists: AcquisitionList[]; update: UpdateInfo | null }
export interface ImportPreview { import_id: string; filename: string; rows: PreviewRow[]; warnings: string[]; headers?: string[]; mapping?: Record<string, unknown> }
export interface Template { id: string; name: string; columns: string[]; warnings?: string[] }
export const emptyBook = (): BookFields => ({ title: "", author: "", publisher: "", isbn: "", price: null, quantity: 1, selected: true, category: "", requester: "", audience: "", priority: "normal", source: "직접 입력", note: "", published_date: "", link: "", needs_review: false, warnings: [] });
export const money = (value: number) => `${new Intl.NumberFormat("ko-KR").format(value)}원`;
// Work in integer basis points so half-won boundaries match the server exactly.
export const orderAmount = (price: number, discount: number, quantity: number) => Math.floor((price * (10_000 - Math.round(discount * 100)) + 5_000) / 10_000) * quantity;
export const errorMessage = (error: unknown) => error instanceof Error ? error.message : "처리하지 못했습니다. 잠시 후 다시 시도해 주세요.";
