import type { SuseoroApi } from "../../api/client";
import type { components } from "../../api/types";

export type ProcurementImport = components["schemas"]["ProcurementImportItem"];

const TERMINAL_JOB_STATES = new Set([
  "SUCCEEDED",
  "PARTIAL",
  "FAILED",
  "CANCELLED",
  "CANCELED",
]);

export const PROCUREMENT_FILE_ACCEPT = [
  ".csv",
  ".tsv",
  ".txt",
  ".xls",
  ".xlsx",
  ".xlsb",
  ".ods",
  ".pdf",
  ".docx",
  ".hwpx",
  ".hwp",
].join(",");

export async function waitForProcurementJob(
  api: SuseoroApi,
  jobId: string,
): Promise<void> {
  for (let attempt = 0; attempt < 40; attempt += 1) {
    const job = await api.getJob(jobId);
    if (TERMINAL_JOB_STATES.has(job.status)) return;
    await new Promise((resolve) => window.setTimeout(resolve, 150));
  }
  throw new Error(
    "파일 처리가 계속 진행 중입니다. 잠시 뒤 이 화면에서 이어서 확인해 주세요.",
  );
}

export function procurementImportMessage(item: ProcurementImport): string {
  switch (item.status) {
    case "READY":
      return "서버가 파일을 읽었습니다. 결과를 반영할 수 있습니다.";
    case "IMPORTED":
    case "IMPORTED_PARTIAL":
      return "서버 처리 결과를 반영했습니다.";
    case "MAPPING_REQUIRED":
      return "열 이름을 확인해야 합니다. 원본 자료 화면에서 열 연결을 완료해 주세요.";
    case "PARTIAL":
      return "일부 행에 오류가 있어 자동 반영하지 않았습니다. 아래 행을 고친 파일을 다시 올려 주세요.";
    case "UNSUPPORTED_FORMAT":
      return "원본은 안전하게 보관했습니다. 이 형식은 표로 변환한 뒤 다시 올려 주세요.";
    case "FAILED":
      return "파일을 읽지 못했습니다. 원본 형식과 필수 열을 확인해 주세요.";
    default:
      return "서버에서 파일을 읽고 있습니다. 이 화면을 다시 열어도 진행 결과가 남습니다.";
  }
}
