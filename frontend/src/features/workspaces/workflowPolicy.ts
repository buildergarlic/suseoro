import type { User } from "../../api/client";

export type WorkroomMode =
  | "INGEST"
  | "ANALYZING"
  | "CANDIDATES"
  | "APPROVAL"
  | "RECEIVING";

export type WorkroomAction =
  | "APPROVE_LIST"
  | "REQUEST_CHANGES"
  | "ADD_QUOTE"
  | "SELECT_QUOTE"
  | "DOWNLOAD_ORDER"
  | "MARK_ORDER_SENT"
  | "ADD_DELIVERY"
  | "START_SCAN"
  | "SCAN_BOOK"
  | "SET_DISPOSITION"
  | "COMPLETE_RECEIVING";

interface WorkflowPolicy {
  label: string;
  stage: 0 | 1 | 2 | 3;
  mode: WorkroomMode;
  owner: "담당자" | "검토자" | "자동 처리" | "완료";
  operatorPriority: number | null;
  reviewerPriority: number | null;
}

const POLICIES: Record<string, WorkflowPolicy> = {
  DRAFT: {
    label: "자료 준비 중",
    stage: 0,
    mode: "INGEST",
    owner: "담당자",
    operatorPriority: 2,
    reviewerPriority: null,
  },
  ANALYZING: {
    label: "도서 비교 중",
    stage: 0,
    mode: "ANALYZING",
    owner: "자동 처리",
    operatorPriority: null,
    reviewerPriority: null,
  },
  CANDIDATE_REVIEW: {
    label: "후보 확인 중",
    stage: 0,
    mode: "CANDIDATES",
    owner: "담당자",
    operatorPriority: 1,
    reviewerPriority: null,
  },
  CHANGES_REQUESTED: {
    label: "수정 요청됨",
    stage: 0,
    mode: "CANDIDATES",
    owner: "담당자",
    operatorPriority: 0,
    reviewerPriority: null,
  },
  APPROVAL_PENDING: {
    label: "승인 기다리는 중",
    stage: 1,
    mode: "APPROVAL",
    owner: "검토자",
    operatorPriority: null,
    reviewerPriority: 0,
  },
  APPROVED: {
    label: "승인됨",
    stage: 1,
    mode: "APPROVAL",
    owner: "담당자",
    operatorPriority: 3,
    reviewerPriority: null,
  },
  QUOTE_REVIEW: {
    label: "견적·예산 조정 중",
    stage: 1,
    mode: "APPROVAL",
    owner: "담당자",
    operatorPriority: 4,
    reviewerPriority: null,
  },
  ORDER_READY: {
    label: "발주파일 준비됨",
    stage: 1,
    mode: "APPROVAL",
    owner: "담당자",
    operatorPriority: 5,
    reviewerPriority: null,
  },
  ORDER_SENT: {
    label: "납품 기다리는 중",
    stage: 2,
    mode: "RECEIVING",
    owner: "담당자",
    operatorPriority: 6,
    reviewerPriority: null,
  },
  RECEIVING: {
    label: "납품 검수 중",
    stage: 2,
    mode: "RECEIVING",
    owner: "담당자",
    operatorPriority: 7,
    reviewerPriority: null,
  },
  COMPLETED: {
    label: "완료",
    stage: 3,
    mode: "RECEIVING",
    owner: "완료",
    operatorPriority: null,
    reviewerPriority: null,
  },
};

const FALLBACK: WorkflowPolicy = {
  label: "상태 확인 필요",
  stage: 0,
  mode: "ANALYZING",
  owner: "자동 처리",
  operatorPriority: null,
  reviewerPriority: null,
};

const ACTION_STATES: Record<WorkroomAction, readonly string[]> = {
  APPROVE_LIST: ["APPROVAL_PENDING"],
  REQUEST_CHANGES: ["APPROVAL_PENDING"],
  ADD_QUOTE: ["APPROVED", "QUOTE_REVIEW"],
  SELECT_QUOTE: ["QUOTE_REVIEW"],
  DOWNLOAD_ORDER: ["ORDER_READY"],
  MARK_ORDER_SENT: ["ORDER_READY"],
  ADD_DELIVERY: ["ORDER_SENT", "RECEIVING"],
  START_SCAN: ["ORDER_SENT", "RECEIVING"],
  SCAN_BOOK: ["RECEIVING"],
  SET_DISPOSITION: ["RECEIVING"],
  COMPLETE_RECEIVING: ["RECEIVING"],
};

const REVIEWER_ACTIONS = new Set<WorkroomAction>([
  "APPROVE_LIST",
  "REQUEST_CHANGES",
]);

export function workflowPolicy(status: string): WorkflowPolicy {
  return POLICIES[status] ?? FALLBACK;
}

export function isReviewerOnly(user: User): boolean {
  return user.roles.includes("REVIEWER") && !user.roles.includes("OPERATOR");
}

export function primaryPriority(user: User, status: string): number | null {
  const policy = workflowPolicy(status);
  return isReviewerOnly(user)
    ? policy.reviewerPriority
    : user.roles.includes("OPERATOR")
      ? policy.operatorPriority
      : null;
}

export function canOperate(user: User): boolean {
  return user.roles.includes("OPERATOR");
}

export function canPerformAction(
  user: User,
  status: string,
  action: WorkroomAction,
): boolean {
  const role = REVIEWER_ACTIONS.has(action) ? "REVIEWER" : "OPERATOR";
  return user.roles.includes(role) && ACTION_STATES[action].includes(status);
}
