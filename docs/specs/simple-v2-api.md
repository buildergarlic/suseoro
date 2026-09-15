# Simple API contract

Prefix `/api/library`. JSON except upload/download. Errors `{detail: "Korean readable message"}`. Fetch bootstrap before mutations and send returned `csrf_token` as `X-Suseoro-Token`. Same-origin loopback only; desktop launches protected local server. No user login.

## Shapes
`List`: `{id:string,name:string,year:number,budget:number,discount_percent:number,created_at:string}`.
`Book`: `{id:string,title:string,author:string,publisher:string,isbn:string,price:number|null,quantity:number,selected:boolean,category:string,requester:string,audience:string,priority:"high"|"normal"|"low",source:string,note:string,published_date:string,link:string,needs_review:boolean,warnings:string[],held:boolean,duplicate:boolean}`.
`Summary`: `{selected_count:number,total_quantity:number,list_total:number,order_total:number,remaining:number,missing_price_count:number,review_count:number,held_count:number,duplicate_count:number}`. Currency integer KRW; round half up per unit after discount then multiply quantity. Missing prices excluded from numeric totals AND separately counted; final export rejects missing price/title/ISBN-invalid, unresolved review and over-budget selections.
`PreviewRow`: same editable fields as Book without id/held/duplicate; optional raw text/source provenance, needs_review/warnings.

## Endpoints
- `GET /bootstrap` → `{version,csrf_token,settings:{school_name,nl_api_key_configured},lists:List[],update:{...}|null}`. Always at least one list on new install.
- `PATCH /settings` body `{school_name?,nl_api_key?}` → safe settings; secret never returned.
- `POST /lists` body `{name,year?,budget?:15000000,discount_percent?:0}` → List.
- `GET /lists/{id}` → `{list:List,books:Book[],summary:Summary}`.
- `PATCH /lists/{id}` same editable list fields → List.
- `POST /lists/{id}/books` body Book fields → Book; manual blank ISBN allowed but title required or needs_review.
- `PATCH /lists/{id}/books/{bookId}` partial fields → Book.
- `DELETE /lists/{id}/books/{bookId}` → `{ok:true}` soft-delete.
- `POST /lists/{id}/books/{bookId}/restore` → Book.
- `POST /lookup` body `{isbn}` → `{found,book?,warnings:string[]}`. No auto-save.
- `POST /imports/preview` multipart `file` → `{import_id,filename,rows:PreviewRow[],warnings:string[],headers?:string[],mapping?:object}`.
- `POST /lists/{id}/imports` JSON `{import_id,rows:PreviewRow[],kind:"recommendations"|"holdings"}` → `{added:number,warnings:string[]}`. Preview user edits included; imports permit unpriced/unresolved with needs_review.
- `POST /templates` multipart `file` → `{id,name,columns:string[],warnings:string[]}` for XLSX school template.
- `GET /templates` → array of template summaries.
- `POST /lists/{id}/export` JSON `{format:"xlsx"|"csv"|"html",template_id?:string}` → binary download with Content-Disposition filename. All selected books in order.
- `GET /backup` → JSON backup download (no secrets).
- `POST /restore` multipart `file` → `{ok:true}`; schema validate, pre-restore backup, restores lists/books/holdings/settings excluding secrets.
- `GET /updates` → `{current_version,latest_version?,available:boolean,url?,message?}`. Official GitHub stable releases only.
- `POST /updates/install` → `{started:true}` when desktop installer handoff available; browser fallback link.

Frontend reloads list after mutations and displays backend errors without losing unsaved edits. API client downloads files using blobs so token header is included. Frontend agent may propose contract corrections before changing it.
