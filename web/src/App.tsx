import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';

type Screen = 'home' | 'upload' | 'processing' | 'review' | 'results';
type Role = 'policy_wording' | 'policy_schedule' | 'bill' | 'settlement';
type ReviewAction = 'CONFIRM' | 'CORRECT' | 'UNRESOLVED';
type VerificationMethod = 'EVIDENCE_SPAN' | 'HUMAN_VISUAL' | 'UNRESOLVED';

type CaseSummary = {
  case_id: string;
  display_name: string | null;
  status: string;
  latest_analysis_run_id: string | null;
  input_revision: number;
};
type CaseListResponse = { items: CaseSummary[]; next_cursor: string | null };
type DocumentSummary = {
  document_id: string;
  case_id: string;
  role: Role;
  original_filename: string;
  media_type: string;
  byte_size: number;
  page_count: number;
  state: string;
  uploaded_at: string;
  selected_for_role: boolean;
};
type DocumentListResponse = { items: DocumentSummary[]; next_cursor: string | null };
type UploadResponse = {
  document: DocumentSummary;
  job: { job_id: string; status: string; stage: string; document_id: string | null };
};
type ProcessingRun = { status: string; error_code: string | null; detected_role: Role | null };
type ProcessingDocument = {
  document_id: string;
  assigned_role: Role;
  selected_for_role: boolean;
  state: string;
  latest_run: ProcessingRun | null;
};
type ProcessingStatus = {
  case_id: string;
  status: string;
  document_count: number;
  documents: ProcessingDocument[];
  job_counts: Record<string, number>;
  active_jobs: number;
  ready_for_analysis: boolean;
};
type ReviewGap = {
  code: string;
  message: string;
  role: Role | null;
  field_path: string | null;
  document_id: string | null;
  processing_run_id: string | null;
  blocking: boolean;
};
type Readiness = {
  case_id: string;
  status: 'READY_FOR_ANALYSIS' | 'NEEDS_REVIEW' | 'EVIDENCE_GAP' | 'ROLE_GAP';
  input_revision: number;
  ready_for_analysis: boolean;
  selected_roles: Record<Role, string | null>;
  gaps: ReviewGap[];
};
type ReviewItem = {
  item_id: string;
  kind: string;
  code: string;
  role: Role | null;
  document_id: string | null;
  processing_run_id: string | null;
  field_path: string | null;
  candidate_value: unknown;
  candidate_state: string | null;
  candidate_evidence_id: string | null;
  latest_correction_action: string | null;
  reason: string | null;
  message: string;
};
type ReviewQueue = {
  case_id: string;
  status: Readiness['status'];
  input_revision: number;
  items: ReviewItem[];
  warnings: ReviewGap[];
};
type EvidenceDetail = {
  evidence_id: string;
  document_id: string;
  document_role: Role;
  page_number: number;
  verification_status: string;
  quoted_text: string;
};
type AnalysisFinding = {
  finding_id: string;
  type: 'FINANCIAL' | 'PROCEDURAL' | 'EVIDENCE_GAP' | 'INFO';
  state: 'CONSISTENT' | 'POTENTIALLY_INCONSISTENT' | 'UNDETERMINED';
  head: string;
  amount_paise: number | null;
  calculation_step_ids: string[];
};
type AnalysisResult = {
  verdict: { state: AnalysisFinding['state']; head_states: Record<string, AnalysisFinding['state']> };
  reconciliation: {
    lawful_payable_paise: number;
    paid_paise: number;
    difference_paise: number;
    supported_difference_paise: number;
    unexplained_paise: number;
    identity_ok: boolean;
  };
  calculation: {
    gross_bill_paise: number;
    lawful_payable_paise: number;
    patient_share_paise: number;
    reductions_paise: number;
    steps: Array<{ step_id: string; step_type: string; output_paise: number }>;
  };
  findings: AnalysisFinding[];
  withheld_findings_count: number;
  rulepack_version: string;
  corpus_snapshot_id: string | null;
};
type AnalysisRun = {
  analysis_run_id: string;
  case_id: string;
  run_number: number;
  input_revision: number | null;
  status: 'QUEUED' | 'RUNNING' | 'SUCCEEDED' | 'FAILED' | 'CANCELLED';
  error_code: string | null;
  created_at: string;
  completed_at: string | null;
  result: AnalysisResult | null;
};
type AnalysisRunResponse = { analysis_run: AnalysisRun };
type AnalysisListResponse = {
  case_id: string;
  current_input_revision: number;
  items: AnalysisRun[];
};
type UploadSlot = {
  status: 'idle' | 'uploading' | 'received' | 'error' | 'removed';
  fileName?: string;
  progress?: number;
  document?: DocumentSummary;
  message?: string;
};
type ReviewedSource = {
  key: string;
  documentId: string;
  role: Role;
  pageNumber: number;
  evidenceId: string | null;
  fieldLabel: string;
  quote: string | null;
};

const API = '/api/v1';
const MAX_UPLOAD_BYTES = 25 * 1024 * 1024;
const SESSION_CASE_KEY = 'claimcheck.current-case';
const ROLES: Role[] = ['policy_wording', 'policy_schedule', 'bill', 'settlement'];
const ROLE_COPY: Record<Role, { title: string; short: string; icon: string }> = {
  policy_wording: { title: 'Policy wording', short: 'The cover and exclusions', icon: 'book' },
  policy_schedule: { title: 'Policy schedule', short: 'Your limits and details', icon: 'shield' },
  bill: { title: 'Hospital bill', short: 'The charges from your visit', icon: 'receipt' },
  settlement: { title: 'Settlement letter', short: 'The insurer’s payment or decision', icon: 'letter' },
};
const CATEGORY_OPTIONS = [
  'room', 'icu', 'nursing', 'surgeon', 'anaesthesia', 'ot_charges', 'consultation',
  'pharmacy', 'consumables', 'implants', 'medical_devices', 'diagnostics', 'imaging',
  'procedures', 'blood', 'physiotherapy', 'diet', 'documentation', 'administration',
  'package', 'ambulance', 'miscellaneous',
];

class ProductError extends Error {
  code: string;
  status: number;
  constructor(code: string, status: number) {
    super(friendlyError(code, status));
    this.name = 'ProductError';
    this.code = code;
    this.status = status;
  }
}

async function requestJson<T>(path: string, init: RequestInit = {}): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${API}${path}`, {
      credentials: 'same-origin',
      ...init,
      headers: { Accept: 'application/json', ...(init.headers ?? {}) },
    });
  } catch {
    throw new ProductError('NETWORK', 0);
  }
  const data = await response.json().catch(() => null) as Record<string, unknown> | null;
  if (!response.ok) {
    const code = typeof data?.code === 'string' ? data.code : 'REQUEST_FAILED';
    throw new ProductError(code, response.status);
  }
  return data as T;
}

function friendlyError(code: string, status: number): string {
  const messages: Record<string, string> = {
    NETWORK: 'We couldn’t connect just now. Check your connection and try again.',
    UPLOAD_TOO_LARGE: 'This file is too large to upload. Choose a smaller PDF.',
    UNSUPPORTED_FILE_TYPE: 'Please choose a PDF document.',
    CORRUPT_PDF: 'We couldn’t read this PDF. Try another copy of the document.',
    INVALID_PDF: 'We couldn’t read this PDF. Try another copy of the document.',
    INVALID_MULTIPART: 'The file didn’t upload correctly. Please try again.',
    INVALID_UPLOAD_FIELDS: 'The file didn’t upload correctly. Please try again.',
    DUPLICATE_DOCUMENT: 'This same document is already attached under another section.',
    ROLE_SOURCE_NOT_SELECTED: 'Choose this document in the source list before confirming the detail.',
    SOURCE_HASH_MISMATCH: 'This document has changed since it was reviewed. Upload it again to continue.',
    EVIDENCE_DOES_NOT_SUPPORT_VALUE: 'That value doesn’t match the selected text. Open the document and review the page instead.',
    CANDIDATE_CANNOT_BE_CONFIRMED: 'This detail needs a closer look before it can be confirmed.',
    CORRECTION_VALUE_INVALID: 'Enter a valid value for this detail.',
    CORRECTION_VALUE_REQUIRED: 'Enter the corrected value before saving.',
    VISUAL_PAGE_REQUIRED: 'Open the document and confirm the page you checked.',
    TRUSTED_INPUT_NOT_READY: 'A few document details still need your attention.',
    ANALYSIS_ALREADY_RUNNING: 'Your review is already being prepared. Please wait a moment.',
    ANALYSIS_INPUT_CHANGED: 'Your documents changed during the review. Please refresh and try again.',
    ANALYSIS_FAILED: 'We couldn’t complete this review. Your documents are safe; you can try again.',
    NOT_FOUND: 'We couldn’t find this review. It may no longer be available.',
    INVALID_DOCUMENT_ROLE: 'Choose the matching document section and try again.',
    DOCUMENT_PROCESSING_ACTIVE: 'This document is still being checked. Try again when it’s ready.',
    DOCUMENT_UNAVAILABLE: 'This document is no longer available. Upload it again to continue.',
  };
  if (messages[code]) return messages[code];
  if (status === 401 || status === 403) return 'Please sign in again to continue your review.';
  if (status === 404) return 'We couldn’t find this review. It may no longer be available.';
  if (status >= 500 || status === 0) return 'Something went wrong on our side. Please try again shortly.';
  return 'We couldn’t save that just now. Please check the details and try again.';
}

function uploadWithProgress(
  caseId: string,
  role: Role,
  file: File,
  onProgress: (value: number) => void,
): Promise<UploadResponse> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    const form = new FormData();
    form.append('file', file);
    form.append('role', role);
    xhr.open('POST', `${API}/cases/${encodeURIComponent(caseId)}/documents`);
    xhr.withCredentials = true;
    xhr.setRequestHeader('Accept', 'application/json');
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable) onProgress(Math.min(99, Math.round(event.loaded / event.total * 100)));
    };
    xhr.onerror = () => reject(new ProductError('NETWORK', 0));
    xhr.onabort = () => reject(new ProductError('NETWORK', 0));
    xhr.onload = () => {
      let body: Record<string, unknown> | null = null;
      try { body = JSON.parse(xhr.responseText) as Record<string, unknown>; } catch { body = null; }
      if (xhr.status < 200 || xhr.status >= 300) {
        reject(new ProductError(typeof body?.code === 'string' ? body.code : 'REQUEST_FAILED', xhr.status));
        return;
      }
      onProgress(100);
      resolve(body as unknown as UploadResponse);
    };
    xhr.send(form);
  });
}

function encodeBody(value: unknown): string {
  return JSON.stringify(value);
}

function formatMoney(paise: number): string {
  return new Intl.NumberFormat('en-IN', {
    style: 'currency', currency: 'INR', minimumFractionDigits: 0, maximumFractionDigits: 2,
  }).format(paise / 100);
}

function parseMoneyInput(value: string): number | null {
  const normalized = value.trim().replace(/[,₹\s]/g, '');
  if (!/^\d{1,12}(?:\.\d{1,2})?$/.test(normalized)) return null;
  const [rupees, fraction = ''] = normalized.split('.');
  const paise = Number(rupees) * 100 + Number((fraction + '00').slice(0, 2));
  return Number.isSafeInteger(paise) ? paise : null;
}

function displayRole(role: Role | null | undefined): string {
  return role ? ROLE_COPY[role]?.title ?? 'Document' : 'Your document';
}

function humanField(path: string | null): string {
  if (!path) return 'Document detail';
  const fixed: Record<string, string> = {
    'policy.sum_insured_paise': 'Policy cover amount',
    'policy.room_rent_limit_paise': 'Room charge limit',
    'policy.co_pay_percent': 'Co-payment percentage',
    'policy.ame_definition_present': 'Additional expense definition',
    'policy.claim_date': 'Claim date',
    'policy.inception_date': 'Policy start date',
    'policy.renewal_date': 'Policy renewal date',
    'policy.uin': 'Policy identification number',
    'policy.product': 'Policy name',
    'policy.effective_text': 'Policy term',
    'bill.total_paise': 'Hospital bill total',
    'settlement.claimed_amount_paise': 'Amount claimed',
    'settlement.final_payable_paise': 'Amount paid by the insurer',
    'settlement.partial_disallowance': 'Was part of the claim declined?',
    'settlement.repudiated': 'Was the claim declined in full?',
  };
  if (fixed[path]) return fixed[path];
  if (/^bill\.line\..*\.amount_paise$/.test(path)) return 'Hospital charge amount';
  if (/^bill\.line\..*\.category$/.test(path)) return 'Type of hospital charge';
  if (/^settlement\.line\..*\.amount_paise$/.test(path)) return 'Settlement deduction amount';
  if (/^settlement\.line\..*\.head$/.test(path)) return 'Settlement deduction type';
  return 'Claim detail';
}

function fieldKind(path: string | null): 'money' | 'percent' | 'boolean' | 'category' | 'date' | 'text' | 'head' {
  if (!path) return 'text';
  if (/paise$/.test(path)) return 'money';
  if (path.endsWith('co_pay_percent')) return 'percent';
  if (path.endsWith('category')) return 'category';
  if (path.endsWith('.head')) return 'head';
  if (path.endsWith('partial_disallowance') || path.endsWith('repudiated') || path.endsWith('ame_definition_present')) return 'boolean';
  if (path.endsWith('_date')) return 'date';
  return 'text';
}

function requiresVisual(path: string | null): boolean {
  const kind = fieldKind(path);
  return kind !== 'money' && kind !== 'percent' && kind !== 'text';
}

function formatCandidate(item: ReviewItem): string {
  const value = item.candidate_value;
  if (value === null || value === undefined) return 'Not found yet';
  const kind = fieldKind(item.field_path);
  if (kind === 'money' && typeof value === 'number') return formatMoney(value);
  if (kind === 'percent') return `${String(value)}%`;
  if (kind === 'boolean') return value === true ? 'Yes' : value === false ? 'No' : 'Not clear';
  if (kind === 'category') return humanize(String(value));
  if (kind === 'date') return String(value);
  return String(value);
}

function humanize(value: string): string {
  return value.replaceAll('_', ' ').replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function stateLabel(state: AnalysisFinding['state']): string {
  if (state === 'CONSISTENT') return 'Appears supported';
  if (state === 'POTENTIALLY_INCONSISTENT') return 'May need clarification';
  return 'Couldn’t verify yet';
}

function verdictCopy(state: AnalysisFinding['state']): { title: string; body: string; tone: string } {
  if (state === 'CONSISTENT') return {
    title: 'The details we could verify appear supported',
    body: 'This is based on the documents available and the details confirmed during your review.',
    tone: 'supported',
  };
  if (state === 'POTENTIALLY_INCONSISTENT') return {
    title: 'Some deductions may need clarification',
    body: 'We found a difference worth asking about. This review is not a decision about whether the insurer was right or wrong.',
    tone: 'question',
  };
  return {
    title: 'We couldn’t verify every detail yet',
    body: 'Some information was missing or unclear. Check the source pages below before deciding what to do next.',
    tone: 'uncertain',
  };
}

function findingDescription(finding: AnalysisFinding): string {
  if (finding.type === 'EVIDENCE_GAP' || finding.state === 'UNDETERMINED') {
    return 'We couldn’t verify this detail yet from the documents available.';
  }
  if (finding.state === 'POTENTIALLY_INCONSISTENT') {
    return 'The details available for this review show a difference that may be worth clarifying.';
  }
  return 'This detail appears supported by the information you shared.';
}

function nextStep(finding: AnalysisFinding): string {
  if (finding.state === 'POTENTIALLY_INCONSISTENT') {
    return 'Ask the insurer to explain how this amount was applied and which policy term they used.';
  }
  if (finding.state === 'UNDETERMINED' || finding.type === 'EVIDENCE_GAP') {
    return 'Check the cited page or add a clearer copy of the document if you have one.';
  }
  return 'Keep the relevant policy and settlement pages with your claim records.';
}

function findingTitle(head: string): string {
  const key = head.toLowerCase();
  if (key.includes('room')) return 'Room charge';
  if (key.includes('copay') || key.includes('co_pay')) return 'Co-payment';
  if (key.includes('non_payable')) return 'Non-payable amount';
  if (key.includes('sum_insured') || key.includes('limit')) return 'Policy limit';
  if (key.includes('bill')) return 'Hospital bill';
  if (key.includes('settlement')) return 'Settlement amount';
  if (key.includes('identity')) return 'Amount reconciliation';
  return humanize(head);
}

function documentSlotStates(): Record<Role, UploadSlot> {
  return {
    policy_wording: { status: 'idle' },
    policy_schedule: { status: 'idle' },
    bill: { status: 'idle' },
    settlement: { status: 'idle' },
  };
}

function App() {
  const [screen, setScreen] = useState<Screen>('home');
  const [caseId, setCaseId] = useState<string | null>(() => {
    try { return window.sessionStorage.getItem(SESSION_CASE_KEY); } catch { return null; }
  });
  const [isExampleCase, setIsExampleCase] = useState(false);
  const [documents, setDocuments] = useState<DocumentSummary[]>([]);
  const [slots, setSlots] = useState<Record<Role, UploadSlot>>(documentSlotStates);
  const [processing, setProcessing] = useState<ProcessingStatus | null>(null);
  const [readiness, setReadiness] = useState<Readiness | null>(null);
  const [reviewQueue, setReviewQueue] = useState<ReviewQueue | null>(null);
  const [activeRun, setActiveRun] = useState<AnalysisRun | null>(null);
  const [analysisHistory, setAnalysisHistory] = useState<AnalysisRun[]>([]);
  const [analysisResult, setAnalysisResult] = useState<AnalysisResult | null>(null);
  const [reviewedSources, setReviewedSources] = useState<ReviewedSource[]>([]);
  const [evidenceCache, setEvidenceCache] = useState<Record<string, EvidenceDetail>>({});
  const [evidenceTarget, setEvidenceTarget] = useState<ReviewItem | ReviewedSource | null>(null);
  const [verifiedPages, setVerifiedPages] = useState<Record<string, number>>({});
  const [correctingItem, setCorrectingItem] = useState<string | null>(null);
  const [draftValues, setDraftValues] = useState<Record<string, string>>({});
  const [cardMessages, setCardMessages] = useState<Record<string, string>>({});
  const [pageError, setPageError] = useState<string | null>(null);
  const [homeMessage, setHomeMessage] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [homeAction, setHomeAction] = useState<'start' | 'example' | 'resume' | null>(null);
  const [reviewLoading, setReviewLoading] = useState(false);
  const [processingMessage, setProcessingMessage] = useState<string | null>(null);
  const [processingPollVersion, setProcessingPollVersion] = useState(0);
  const [slowProcessing, setSlowProcessing] = useState(false);
  const [currentUploadName, setCurrentUploadName] = useState<string | null>(null);
  const [removingRole, setRemovingRole] = useState<Role | null>(null);
  const fileInputs = useRef<Partial<Record<Role, HTMLInputElement | null>>>({});
  const resumeBypass = useRef<string | null>(null);

  const syncDocuments = useCallback(async (id: string) => {
    const response = await requestJson<DocumentListResponse>(`/cases/${encodeURIComponent(id)}/documents?limit=100`);
    setDocuments(response.items);
    return response.items;
  }, []);

  const loadReview = useCallback(async (id: string) => {
    setReviewLoading(true);
    setPageError(null);
    try {
      const [ready, queue, docs] = await Promise.all([
        requestJson<Readiness>(`/cases/${encodeURIComponent(id)}/readiness`),
        requestJson<ReviewQueue>(`/cases/${encodeURIComponent(id)}/review-queue`),
        requestJson<DocumentListResponse>(`/cases/${encodeURIComponent(id)}/documents?limit=100`),
      ]);
      setReadiness(ready);
      setReviewQueue(queue);
      setDocuments(docs.items);
      if (ready.ready_for_analysis && queue.items.length === 0) {
        setPageError(null);
      }
      return { ready, queue, docs: docs.items };
    } catch (error) {
      setPageError(error instanceof Error ? error.message : friendlyError('REQUEST_FAILED', 0));
      return null;
    } finally {
      setReviewLoading(false);
    }
  }, []);

  const openResult = useCallback(async (id: string, runId?: string): Promise<boolean> => {
    setLoading(true);
    setPageError(null);
    try {
      const history = await requestJson<AnalysisListResponse>(`/cases/${encodeURIComponent(id)}/analysis`);
      setAnalysisHistory(history.items);
      const current = runId
        ? history.items.find((run) => run.analysis_run_id === runId)
        : history.items.find((run) => run.input_revision === history.current_input_revision && run.status === 'SUCCEEDED');
      if (!current || current.status !== 'SUCCEEDED' || current.input_revision !== history.current_input_revision) {
        throw new ProductError('NOT_FOUND', 404);
      }
      const detail = await requestJson<AnalysisRunResponse>(
        `/cases/${encodeURIComponent(id)}/analysis/${encodeURIComponent(current.analysis_run_id)}`,
      );
      setActiveRun(detail.analysis_run);
      setAnalysisResult(detail.analysis_run.result);
      setScreen('results');
      return true;
    } catch (error) {
      const message = error instanceof Error ? error.message : friendlyError('REQUEST_FAILED', 0);
      setPageError(message);
      setHomeMessage(message);
      return false;
    } finally {
      setLoading(false);
    }
  }, []);

  const openExample = useCallback(async () => {
    setLoading(true);
    setHomeAction('example');
    setHomeMessage(null);
    setIsExampleCase(false);
    try {
      const cases = await requestJson<CaseListResponse>('/cases?limit=100');
      const example = cases.items.find((item) =>
        item.display_name?.trim().toLowerCase() === 'claimcheck example' && item.latest_analysis_run_id,
      );
      if (!example?.latest_analysis_run_id) {
        setHomeMessage('An example review isn’t connected here yet. You can still upload your own documents to begin.');
        return;
      }
      setIsExampleCase(true);
      resumeBypass.current = example.case_id;
      setCaseId(example.case_id);
      try { window.sessionStorage.setItem(SESSION_CASE_KEY, example.case_id); } catch { /* optional */ }
      await syncDocuments(example.case_id);
      const opened = await openResult(example.case_id);
      if (!opened) {
        setIsExampleCase(false);
        setHomeMessage('This example review is not available right now. You can start a new review instead.');
      }
    } catch (error) {
      setIsExampleCase(false);
      setHomeMessage(error instanceof Error ? error.message : friendlyError('REQUEST_FAILED', 0));
    } finally {
      setLoading(false);
      setHomeAction(null);
    }
  }, [openResult, syncDocuments]);

  const resumeCase = useCallback(async (id: string) => {
    setLoading(true);
    setHomeAction('resume');
    try {
      const [summary, docs, state] = await Promise.all([
        requestJson<CaseSummary>(`/cases/${encodeURIComponent(id)}`),
        requestJson<DocumentListResponse>(`/cases/${encodeURIComponent(id)}/documents?limit=100`),
        requestJson<ProcessingStatus>(`/cases/${encodeURIComponent(id)}/processing-status`),
      ]);
      setIsExampleCase(summary.display_name?.trim().toLowerCase() === 'claimcheck example');
      setDocuments(docs.items);
      setProcessing(state);
      let history: AnalysisListResponse | null = null;
      try {
        history = await requestJson<AnalysisListResponse>(`/cases/${encodeURIComponent(id)}/analysis`);
        setAnalysisHistory(history.items);
      } catch {
        setAnalysisHistory([]);
      }
      if (summary.latest_analysis_run_id && history) {
        const latest = history.items.find((run) => run.analysis_run_id === summary.latest_analysis_run_id);
        if (latest?.status === 'SUCCEEDED' && latest.input_revision === history.current_input_revision) {
          await openResult(id, latest.analysis_run_id);
          return;
        }
      }
      if (state.active_jobs > 0) {
        setScreen('processing');
      } else if (docs.items.length > 0) {
        await loadReview(id);
        setScreen('review');
      } else {
        setScreen('upload');
      }
    } catch (error) {
      setCaseId(null);
      setIsExampleCase(false);
      setHomeMessage(error instanceof Error ? error.message : friendlyError('REQUEST_FAILED', 0));
      try { window.sessionStorage.removeItem(SESSION_CASE_KEY); } catch { /* optional */ }
      setScreen('home');
    } finally {
      setLoading(false);
      setHomeAction(null);
    }
  }, [loadReview, openResult]);

  useEffect(() => {
    if (!caseId) return;
    if (resumeBypass.current === caseId) {
      resumeBypass.current = null;
      return;
    }
    void resumeCase(caseId);
    // Restoration occurs once for the active case; the callback itself is stable for its dependencies.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [caseId]);

  const createCase = useCallback(async () => {
    setLoading(true);
    setHomeAction('start');
    setIsExampleCase(false);
    setHomeMessage(null);
    setPageError(null);
    try {
      const created = await requestJson<CaseSummary>('/cases', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: encodeBody({ display_name: 'My claim review' }),
      });
      setDocuments([]);
      setSlots(documentSlotStates());
      setReviewQueue(null);
      setReadiness(null);
      setAnalysisResult(null);
      setActiveRun(null);
      setAnalysisHistory([]);
      setProcessing(null);
      setReviewedSources([]);
      setEvidenceCache({});
      setEvidenceTarget(null);
      setVerifiedPages({});
      setCorrectingItem(null);
      setDraftValues({});
      setCardMessages({});
      resumeBypass.current = created.case_id;
      setCaseId(created.case_id);
      try { window.sessionStorage.setItem(SESSION_CASE_KEY, created.case_id); } catch { /* optional */ }
      setScreen('upload');
    } catch (error) {
      setHomeMessage(error instanceof Error ? error.message : friendlyError('REQUEST_FAILED', 0));
    } finally {
      setLoading(false);
      setHomeAction(null);
    }
  }, []);

  const uploadFile = useCallback(async (role: Role, file: File) => {
    if (!caseId) return;
    const isPdf = file.type === 'application/pdf' || file.name.toLowerCase().endsWith('.pdf');
    if (!isPdf) {
      setSlots((previous) => ({ ...previous, [role]: {
        status: 'error', fileName: file.name, message: 'Please choose a PDF document.',
      } }));
      return;
    }
    if (file.size === 0 || file.size > MAX_UPLOAD_BYTES) {
      setSlots((previous) => ({ ...previous, [role]: {
        status: 'error', fileName: file.name,
        message: file.size === 0 ? 'This file is empty. Choose another PDF.' : 'This file is over the 25 MB limit. Choose a smaller PDF.',
      } }));
      return;
    }
    setCurrentUploadName(file.name);
    setSlots((previous) => ({ ...previous, [role]: {
      status: 'uploading', fileName: file.name, progress: 0,
    } }));
    try {
      const response = await uploadWithProgress(caseId, role, file, (progress) => {
        setSlots((previous) => ({ ...previous, [role]: {
          ...previous[role], status: 'uploading', fileName: file.name, progress,
        } }));
      });
      setSlots((previous) => ({ ...previous, [role]: {
        status: 'received', fileName: file.name, progress: 100, document: response.document,
      } }));
      await requestJson(`/cases/${encodeURIComponent(caseId)}/document-role-assignments`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: encodeBody({ role, document_id: response.document.document_id, confirm_mismatch: false }),
      });
      await syncDocuments(caseId);
    } catch (error) {
      setSlots((previous) => ({ ...previous, [role]: {
        ...previous[role], status: 'error', fileName: file.name,
        message: error instanceof Error ? error.message : friendlyError('REQUEST_FAILED', 0),
      } }));
    } finally {
      setCurrentUploadName(null);
    }
  }, [caseId, syncDocuments]);

  const removeUploadedDocument = useCallback(async (role: Role, documentId: string) => {
    if (!caseId || removingRole) return;
    setRemovingRole(role);
    setPageError(null);
    try {
      const result = await requestJson<{ document_id: string; state: string }>(
        `/cases/${encodeURIComponent(caseId)}/documents/${encodeURIComponent(documentId)}`,
        { method: 'DELETE' },
      );
      setDocuments((previous) => previous.map((document) => document.document_id === documentId
        ? { ...document, selected_for_role: false, state: result.state }
        : document));
      setSlots((previous) => ({
        ...previous,
        [role]: {
          status: 'removed',
          message: 'Removal requested. You can add another PDF while the file is cleared from your review.',
        },
      }));
      await syncDocuments(caseId);
      try {
        const status = await requestJson<ProcessingStatus>(`/cases/${encodeURIComponent(caseId)}/processing-status`);
        setProcessing(status);
      } catch {
        // The removal request is already accepted; the next status refresh will pick up the cleanup job.
      }
    } catch (error) {
      setPageError(error instanceof Error ? error.message : friendlyError('REQUEST_FAILED', 0));
    } finally {
      setRemovingRole(null);
    }
  }, [caseId, removingRole, syncDocuments]);

  const openEvidence = useCallback(async (item: ReviewItem | ReviewedSource) => {
    setEvidenceTarget(item);
    const evidenceId = 'candidate_evidence_id' in item ? item.candidate_evidence_id : item.evidenceId;
    if (!evidenceId || !caseId || evidenceCache[evidenceId]) return;
    try {
      const evidence = await requestJson<EvidenceDetail>(
        `/cases/${encodeURIComponent(caseId)}/evidence/${encodeURIComponent(evidenceId)}`,
      );
      setEvidenceCache((previous) => ({ ...previous, [evidenceId]: evidence }));
    } catch {
      // The document page remains available in the drawer even if a text span cannot be retrieved.
    }
  }, [caseId, evidenceCache]);

  const addReviewedSource = useCallback((item: ReviewItem, pageNumber: number, evidence?: EvidenceDetail) => {
    if (!item.document_id || !item.role) return;
    const key = `${item.item_id}:${pageNumber}`;
    setVerifiedPages((previous) => ({ ...previous, [item.item_id]: pageNumber }));
    setReviewedSources((previous) => {
      if (previous.some((source) => source.key === key)) return previous;
      return [...previous, {
        key,
        documentId: item.document_id as string,
        role: item.role as Role,
        pageNumber,
        evidenceId: evidence?.evidence_id ?? null,
        fieldLabel: humanField(item.field_path),
        quote: evidence?.quoted_text ?? null,
      }];
    });
    setEvidenceTarget(null);
  }, []);

  const refreshReview = useCallback(async () => {
    if (!caseId) return;
    await loadReview(caseId);
  }, [caseId, loadReview]);

  const assignRole = useCallback(async (role: Role, documentId: string, confirmMismatch = false) => {
    if (!caseId) return;
    setPageError(null);
    try {
      const response = await requestJson<{ job: { job_id: string } | null }>(`/cases/${encodeURIComponent(caseId)}/document-role-assignments`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: encodeBody({ role, document_id: documentId, confirm_mismatch: confirmMismatch }),
      });
      await loadReview(caseId);
      if (response.job) setScreen('processing');
    } catch (error) {
      setPageError(error instanceof Error ? error.message : friendlyError('REQUEST_FAILED', 0));
    }
  }, [caseId, loadReview]);

  const submitReview = useCallback(async (item: ReviewItem, action: ReviewAction) => {
    if (!caseId || !item.document_id || !item.processing_run_id || !item.field_path) {
      setCardMessages((previous) => ({ ...previous, [item.item_id]: 'Choose the document source before continuing.' }));
      return;
    }
    const kind = fieldKind(item.field_path);
    const visualPage = verifiedPages[item.item_id];
    let verificationMethod: VerificationMethod = 'UNRESOLVED';
    let evidenceId: string | null = null;
    let pageNumber: number | null = null;
    let correctedValue: unknown = null;

    if (action !== 'UNRESOLVED') {
      if (action === 'CORRECT') {
        const initial = kind === 'money' && typeof item.candidate_value === 'number'
          ? (item.candidate_value / 100).toFixed(2)
          : item.candidate_value === null || item.candidate_value === undefined ? '' : String(item.candidate_value);
        const raw = draftValues[item.item_id] ?? initial;
        if (kind === 'money') {
          correctedValue = parseMoneyInput(raw);
          if (correctedValue === null) {
            setCardMessages((previous) => ({ ...previous, [item.item_id]: 'Enter a valid amount, such as 1250.00.' }));
            return;
          }
        } else if (kind === 'percent') {
          if (!/^(?:100|(?:\d{1,2})(?:\.\d{1,2})?)$/.test(raw.trim())) {
            setCardMessages((previous) => ({ ...previous, [item.item_id]: 'Enter a percentage from 0 to 100.' }));
            return;
          }
          correctedValue = raw.trim();
        } else if (kind === 'boolean') {
          if (raw !== 'true' && raw !== 'false') {
            setCardMessages((previous) => ({ ...previous, [item.item_id]: 'Choose yes or no.' }));
            return;
          }
          correctedValue = raw === 'true';
        } else if (kind === 'category') {
          if (!CATEGORY_OPTIONS.includes(raw)) {
            setCardMessages((previous) => ({ ...previous, [item.item_id]: 'Choose a charge type from the list.' }));
            return;
          }
          correctedValue = raw;
        } else if (kind === 'date') {
          if (!/^\d{4}-\d{2}-\d{2}$/.test(raw)) {
            setCardMessages((previous) => ({ ...previous, [item.item_id]: 'Choose a valid date.' }));
            return;
          }
          correctedValue = raw;
        } else {
          if (!raw.trim()) {
            setCardMessages((previous) => ({ ...previous, [item.item_id]: 'Enter the detail you found in the document.' }));
            return;
          }
          correctedValue = raw.trim();
        }
        if (!visualPage) {
          setCardMessages((previous) => ({ ...previous, [item.item_id]: 'Open the source and confirm the page you checked before saving.' }));
          setEvidenceTarget(item);
          return;
        }
        verificationMethod = 'HUMAN_VISUAL';
        pageNumber = visualPage;
      } else if (requiresVisual(item.field_path) || !item.candidate_evidence_id) {
        if (!visualPage) {
          setCardMessages((previous) => ({ ...previous, [item.item_id]: 'Open the source and confirm the page you checked first.' }));
          setEvidenceTarget(item);
          return;
        }
        verificationMethod = 'HUMAN_VISUAL';
        pageNumber = visualPage;
      } else {
        verificationMethod = 'EVIDENCE_SPAN';
        evidenceId = item.candidate_evidence_id;
      }
    }

    setCardMessages((previous) => ({ ...previous, [item.item_id]: '' }));
    try {
      await requestJson(`/cases/${encodeURIComponent(caseId)}/review-corrections`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: encodeBody({
          document_id: item.document_id,
          processing_run_id: item.processing_run_id,
          field_path: item.field_path,
          action,
          corrected_value: correctedValue,
          evidence_id: evidenceId,
          verification_method: verificationMethod,
          page_number: pageNumber,
          reason_code: verificationMethod === 'HUMAN_VISUAL' ? 'HUMAN_VISUAL_CHECK' : undefined,
        }),
      });
      if (verificationMethod === 'EVIDENCE_SPAN' && item.candidate_evidence_id) {
        const evidence = evidenceCache[item.candidate_evidence_id] ?? await requestJson<EvidenceDetail>(
          `/cases/${encodeURIComponent(caseId)}/evidence/${encodeURIComponent(item.candidate_evidence_id)}`,
        );
        setEvidenceCache((previous) => ({ ...previous, [evidence.evidence_id]: evidence }));
        addReviewedSource(item, evidence.page_number, evidence);
      } else if (verificationMethod === 'HUMAN_VISUAL' && pageNumber) {
        addReviewedSource(item, pageNumber);
      }
      setCorrectingItem(null);
      setDraftValues((previous) => { const next = { ...previous }; delete next[item.item_id]; return next; });
      setCardMessages((previous) => ({ ...previous, [item.item_id]: action === 'UNRESOLVED' ? 'We’ve saved that you couldn’t verify this detail.' : 'Saved. Thank you for checking.' }));
      await refreshReview();
    } catch (error) {
      const productError = error instanceof ProductError ? error : null;
      setCardMessages((previous) => ({
        ...previous,
        [item.item_id]: productError?.code === 'EVIDENCE_DOES_NOT_SUPPORT_VALUE'
          ? 'The selected text doesn’t confirm this detail. Open the original page and review it visually.'
          : error instanceof Error ? error.message : 'We couldn’t save this detail. Please try again.',
      }));
    }
  }, [addReviewedSource, caseId, draftValues, evidenceCache, refreshReview, verifiedPages]);

  const beginProcessing = useCallback(async () => {
    if (!caseId) return;
    setPageError(null);
    setSlowProcessing(false);
    setScreen('processing');
    try {
      const status = await requestJson<ProcessingStatus>(`/cases/${encodeURIComponent(caseId)}/processing-status`);
      setProcessing(status);
      const allReadyForReview = status.active_jobs === 0 && status.documents.length > 0
        && status.documents.every((doc) => doc.latest_run && !['QUEUED', 'RUNNING'].includes(doc.latest_run.status))
        && !status.documents.some((doc) => doc.selected_for_role && ['FAILED', 'SUPERSEDED'].includes(doc.latest_run?.status ?? ''));
      if (allReadyForReview) {
        await loadReview(caseId);
        setScreen('review');
      }
    } catch (error) {
      setPageError(error instanceof Error ? error.message : friendlyError('REQUEST_FAILED', 0));
    }
  }, [caseId, loadReview]);

  useEffect(() => {
    if (screen !== 'processing' || !caseId || analysisResult || processingMessage) return;
    let stopped = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let attempts = 0;
    const poll = async () => {
      try {
        const status = await requestJson<ProcessingStatus>(`/cases/${encodeURIComponent(caseId)}/processing-status`);
        if (stopped) return;
        setProcessing(status);
        setPageError(null);
        attempts += 1;
        setSlowProcessing(attempts >= 12 && status.active_jobs > 0);
        const allTerminal = status.active_jobs === 0 && status.documents.length > 0
          && status.documents.every((doc) => doc.latest_run && !['QUEUED', 'RUNNING'].includes(doc.latest_run.status));
        const selectedFailure = status.documents.some((doc) => doc.selected_for_role && ['FAILED', 'SUPERSEDED'].includes(doc.latest_run?.status ?? ''));
        if (allTerminal) {
          if (selectedFailure) {
            setSlowProcessing(false);
            return;
          }
          await loadReview(caseId);
          if (!stopped) setScreen('review');
          return;
        }
      } catch (error) {
        if (!stopped) setPageError(error instanceof Error ? error.message : friendlyError('REQUEST_FAILED', 0));
      }
      if (!stopped) timer = setTimeout(poll, 1800);
    };
    void poll();
    return () => { stopped = true; if (timer) clearTimeout(timer); };
  }, [analysisResult, caseId, loadReview, processingMessage, processingPollVersion, screen]);

  const analyzeCase = useCallback(async () => {
    if (!caseId) return;
    setPageError(null);
    setProcessingMessage('We’re bringing the details together for your review.');
    setScreen('processing');
    try {
      const submitted = await requestJson<AnalysisRunResponse>(`/cases/${encodeURIComponent(caseId)}/analyze`, {
        method: 'POST',
      });
      const runId = submitted.analysis_run.analysis_run_id;
      const persisted = await requestJson<AnalysisRunResponse>(
        `/cases/${encodeURIComponent(caseId)}/analysis/${encodeURIComponent(runId)}`,
      );
      const list = await requestJson<AnalysisListResponse>(`/cases/${encodeURIComponent(caseId)}/analysis`);
      setAnalysisHistory(list.items);
      setActiveRun(persisted.analysis_run);
      setAnalysisResult(persisted.analysis_run.result);
      if (persisted.analysis_run.input_revision !== list.current_input_revision) {
        await loadReview(caseId);
        setPageError('Your documents changed while the review was being prepared. Please check the latest details and try again.');
        setScreen('review');
        return;
      }
      setScreen('results');
    } catch (error) {
      const message = error instanceof Error ? error.message : friendlyError('REQUEST_FAILED', 0);
      await loadReview(caseId);
      setPageError(message);
      setScreen('review');
    } finally {
      setProcessingMessage(null);
    }
  }, [caseId, loadReview]);

  const retryDocument = useCallback(async (document: DocumentSummary) => {
    if (!caseId) return;
    setPageError(null);
    try {
      await requestJson(`/cases/${encodeURIComponent(caseId)}/documents/${encodeURIComponent(document.document_id)}/reprocess`, {
        method: 'POST',
      });
      setPageError(null);
      setSlowProcessing(false);
      setProcessingPollVersion((version) => version + 1);
      setScreen('processing');
    } catch (error) {
      setPageError(error instanceof Error ? error.message : friendlyError('REQUEST_FAILED', 0));
    }
  }, [caseId]);

  const selectedRoleDocs = useMemo(() => {
    const map = {} as Record<Role, DocumentSummary | undefined>;
    for (const role of ROLES) map[role] = documents.find((doc) => doc.role === role && doc.selected_for_role);
    return map;
  }, [documents]);
  const hasAllRoleDocs = ROLES.every((role) => Boolean(selectedRoleDocs[role]?.document_id));
  const processingBusy = (processing?.active_jobs ?? 0) > 0;
  const currentStep = screen === 'upload' ? 0 : screen === 'processing' ? 1 : screen === 'review' ? 2 : screen === 'results' ? 3 : -1;

  const currentDocument = useMemo(() => {
    if (!evidenceTarget) return undefined;
    const id = 'candidate_evidence_id' in evidenceTarget
      ? evidenceTarget.document_id
      : evidenceTarget.documentId;
    return documents.find((doc) => doc.document_id === id);
  }, [documents, evidenceTarget]);
  const currentEvidence = useMemo(() => {
    if (!evidenceTarget) return undefined;
    const id = 'candidate_evidence_id' in evidenceTarget
      ? evidenceTarget.candidate_evidence_id
      : evidenceTarget.evidenceId;
    return id ? evidenceCache[id] : undefined;
  }, [evidenceCache, evidenceTarget]);

  return (
    <div className="app-root">
      {screen === 'home' ? (
        <Landing
          onStart={() => void createCase()}
          onExample={() => void openExample()}
          onResume={() => { if (caseId) void resumeCase(caseId); }}
          hasResume={Boolean(caseId)}
          loading={loading}
          loadingAction={homeAction}
          message={homeMessage}
        />
      ) : (
        <>
          <AppHeader onHome={() => setScreen('home')} />
          <main className="flow-page">
            <div className="flow-topline">
              <StepProgress current={currentStep} />
            </div>

            {pageError && screen !== 'results' && screen !== 'processing' && (
              <div className="notice-error" role="alert">
                <Icon name="alert" />
                <span>{pageError}</span>
                <button type="button" className="icon-button" onClick={() => setPageError(null)} aria-label="Dismiss message"><Icon name="close" /></button>
              </div>
            )}

            {screen === 'upload' && (
              <div className="screen-transition" key="upload">
                <UploadScreen
                  slots={slots}
                  documents={selectedRoleDocs}
                  fileInputs={fileInputs}
                  currentUploadName={currentUploadName}
                  onFile={(role, file) => void uploadFile(role, file)}
                  onBrowse={(role) => fileInputs.current[role]?.click()}
                  onRemove={(role, documentId) => void removeUploadedDocument(role, documentId)}
                  removingRole={removingRole}
                  onContinue={() => void beginProcessing()}
                  onBack={() => setScreen('home')}
                  canContinue={hasAllRoleDocs && !Object.values(slots).some((slot) => slot.status === 'uploading') && removingRole === null}
                />
              </div>
            )}

            {screen === 'processing' && (
              <div className="screen-transition" key="processing">
                <ProcessingScreen
                  status={processing}
                  documents={documents}
                  busy={loading || processingBusy || Boolean(processingMessage)}
                  slow={slowProcessing}
                  message={processingMessage}
                  pageError={pageError}
                  onRetry={() => void beginProcessing()}
                  onRetryDocument={(document) => void retryDocument(document)}
                  onReplaceDocument={(document) => {
                    void removeUploadedDocument(document.role, document.document_id);
                    setScreen('upload');
                  }}
                  onAssignRole={(role, documentId, confirm) => void assignRole(role, documentId, confirm)}
                  onBack={() => setScreen('upload')}
                />
              </div>
            )}

            {screen === 'review' && (
              <div className="screen-transition" key="review">
                <ReviewScreen
                  readiness={readiness}
                  queue={reviewQueue}
                  documents={documents}
                  loading={reviewLoading}
                  pageError={pageError}
                  analysisHistory={analysisHistory}
                  correctingItem={correctingItem}
                  draftValues={draftValues}
                  cardMessages={cardMessages}
                  verifiedPages={verifiedPages}
                  evidenceCache={evidenceCache}
                  onOpenEvidence={(item) => void openEvidence(item)}
                  onCorrecting={setCorrectingItem}
                  onDraft={(itemId, value) => setDraftValues((previous) => ({ ...previous, [itemId]: value }))}
                  onSubmit={(item, action) => void submitReview(item, action)}
                  onAssign={(role, docId, confirmMismatch) => void assignRole(role, docId, confirmMismatch)}
                  onAddDocuments={() => setScreen('upload')}
                  onRetryDocument={(document) => void retryDocument(document)}
                  onRefresh={() => void refreshReview()}
                  onAnalyze={() => void analyzeCase()}
                />
              </div>
            )}

            {screen === 'results' && (
              <div className="screen-transition" key="results">
                {analysisResult && activeRun ? (
                  <ResultsScreen
                    isExampleCase={isExampleCase}
                    result={analysisResult}
                    runStatus={activeRun.status}
                    currentRun={activeRun}
                    history={analysisHistory}
                    sources={reviewedSources}
                    documents={documents}
                    onOpenSource={(source) => void openEvidence(source)}
                    onNewReview={() => {
                      setCaseId(null);
                      setIsExampleCase(false);
                      setDocuments([]);
                      setSlots(documentSlotStates());
                      setAnalysisResult(null);
                      setActiveRun(null);
                      setAnalysisHistory([]);
                      setReviewQueue(null);
                      setReadiness(null);
                      setProcessing(null);
                      setReviewedSources([]);
                      setEvidenceCache({});
                      setEvidenceTarget(null);
                      setVerifiedPages({});
                      setCorrectingItem(null);
                      setDraftValues({});
                      setCardMessages({});
                      try { window.sessionStorage.removeItem(SESSION_CASE_KEY); } catch { /* optional */ }
                      setScreen('home');
                    }}
                  />
                ) : (
                  <AnalysisFailure onRetry={() => void analyzeCase()} onReview={() => setScreen('review')} />
                )}
              </div>
            )}
          </main>
        </>
      )}

      {evidenceTarget && currentDocument && (
        <EvidenceDrawer
          caseId={caseId ?? ''}
          document={currentDocument}
          evidence={currentEvidence}
          initialPage={currentEvidence?.page_number ?? ('pageNumber' in evidenceTarget ? evidenceTarget.pageNumber : 1)}
          onClose={() => setEvidenceTarget(null)}
          onUse={(page, evidence) => {
            if ('candidate_evidence_id' in evidenceTarget) {
              addReviewedSource(evidenceTarget, page, evidence);
            } else {
              const item = reviewQueue?.items.find((candidate) => candidate.item_id === evidenceTarget.key.split(':').slice(0, -1).join(':'));
              if (item) addReviewedSource(item, page, evidence);
              else setEvidenceTarget(null);
            }
          }}
        />
      )}
    </div>
  );
}

function Landing({
  onStart, onExample, onResume, hasResume, loading, loadingAction, message,
}: { onStart: () => void; onExample: () => void; onResume: () => void; hasResume: boolean; loading: boolean; loadingAction: 'start' | 'example' | 'resume' | null; message: string | null }) {
  return (
    <main className="landing-page">
      <header className="landing-header">
        <Brand />
        <div className="landing-header-right">
          <span className="privacy-inline"><Icon name="lock" /> Your documents stay private</span>
        </div>
      </header>
      <section className="hero-section">
        <div className="hero-copy">
          <p className="eyebrow"><span className="eyebrow-line" /> A clearer view of your claim</p>
          <h1>Understand exactly how your insurance claim was <em>settled.</em></h1>
          <p className="hero-lede">Upload your policy, hospital bill and settlement letter. CLAIMCHECK checks the numbers, policy terms and deductions and explains what it finds.</p>
          <div className="hero-actions">
            <button className="button button-primary button-large" type="button" onClick={onStart} disabled={loading}>
              {loadingAction === 'start' ? 'Starting your review…' : 'Review my claim'}
              {loadingAction === 'start' ? <span className="mini-spinner" /> : <Icon name="arrowRight" />}
            </button>
            <button className="button button-secondary button-large" type="button" onClick={onExample} disabled={loading}>
              {loadingAction === 'example' ? <span className="mini-spinner" /> : null}
              {loadingAction === 'example' ? 'Opening example…' : 'View example claim'}
              {loadingAction !== 'example' && <Icon name="arrowUpRight" />}
            </button>
            {hasResume && <button className="button button-quiet" type="button" onClick={onResume} disabled={loading}>{loadingAction === 'resume' ? 'Opening your review…' : 'Continue your review'} {loadingAction === 'resume' ? <span className="mini-spinner" /> : <Icon name="arrowRight" />}</button>}
            <span className="hero-small-note"><Icon name="spark" /> Clear answers, grounded in your documents</span>
          </div>
          {message && <div className="home-message" role="status"><Icon name="info" />{message}</div>}
          <div className="hero-trust-row">
            <span><Icon name="lock" /> Your documents stay private</span>
            <span className="trust-divider" />
            <span><Icon name="fileCheck" /> You stay in control</span>
          </div>
        </div>
        <div className="hero-art" aria-hidden="true">
          <div className="ambient-orbit orbit-one" />
          <div className="ambient-orbit orbit-two" />
          <div className="hero-paper-shadow" />
          <div className="hero-paper">
            <div className="paper-topline"><span className="paper-dot" /><span>YOUR CLAIM, MADE CLEARER</span><span className="paper-menu">···</span></div>
            <div className="paper-title">A clearer picture<br />of your settlement</div>
            <div className="paper-rule" />
            <div className="paper-row"><span>Hospital bill</span><b>In review</b></div>
            <div className="paper-row"><span>Insurer payment</span><b>In review</b></div>
            <div className="paper-row paper-row-highlight"><span>What may need a closer look</span><span className="paper-spark">✳</span></div>
            <div className="paper-progress"><span /></div>
            <div className="paper-foot"><Icon name="shield" /> Based on the documents you share</div>
          </div>
          <div className="floating-note floating-note-top"><span className="floating-icon mint"><Icon name="check" /></span><span><b>Policy terms</b><small>Carefully reviewed</small></span></div>
          <div className="floating-note floating-note-bottom"><span className="floating-icon coral"><Icon name="spark" /></span><span><b>Clear next steps</b><small>Always yours to decide</small></span></div>
          <div className="hero-art-caption">A thoughtful second look at your claim</div>
        </div>
      </section>
      <section className="how-section">
        <div className="how-content">
          <div className="how-heading"><span>01 — 03</span><h2>From paperwork to a clearer picture.</h2><p>Built for the questions that matter when a claim doesn’t add up.</p></div>
          <div className="how-steps">
            <article><span className="how-number">01</span><span className="how-icon"><Icon name="upload" /></span><h3>Share your documents</h3><p>Policy, hospital bill and the insurer’s settlement letter.</p></article>
            <article><span className="how-number">02</span><span className="how-icon"><Icon name="eye" /></span><h3>Check what we found</h3><p>You confirm important details before the review is complete.</p></article>
            <article><span className="how-number">03</span><span className="how-icon"><Icon name="sun" /></span><h3>See what to ask next</h3><p>Understand the amounts, the supporting pages and what remains uncertain.</p></article>
          </div>
        </div>
        <div className="how-image-container">
          <img src="/images/claimcheck/pexels-mikhail-nilov-7731326.jpg" alt="Hands reviewing printed documents" className="how-image" />
        </div>
      </section>
      <footer className="landing-footer"><Brand /><span>Clarity for the moments that matter.</span><span><Icon name="lock" /> Private by design</span></footer>
    </main>
  );
}

function AppHeader({ onHome }: { onHome: () => void }) {
  return (
    <header className="app-header">
      <button type="button" className="brand-button" onClick={onHome} aria-label="CLAIMCHECK home"><Brand /></button>
      <span className="header-case-name">Your claim review</span>
      <span className="header-assurance"><Icon name="lock" /> Your documents stay private</span>
      <span className="header-sparkle"><Icon name="spark" /></span>
    </header>
  );
}

function Brand() {
  return <span className="brand-lockup"><span className="brand-symbol"><span /><span /><span /><span /></span><span className="brand-name">CLAIMCHECK</span></span>;
}

function StepProgress({ current }: { current: number }) {
  const steps = ['Documents', 'Checking', 'Your review', 'Results'];
  return (
    <nav className="step-progress" aria-label="Your claim review">
      {steps.map((step, index) => (
        <div className={`step-item ${index < current ? 'is-done' : ''} ${index === current ? 'is-current' : ''}`} key={step} aria-current={index === current ? 'step' : undefined}>
          <span className="step-dot">{index < current ? <Icon name="check" /> : <span>{String(index + 1).padStart(2, '0')}</span>}</span>
          <span>{step}</span>
          {index < steps.length - 1 && <i className="step-connector" />}
        </div>
      ))}
    </nav>
  );
}

function UploadScreen({
  slots, documents, fileInputs, currentUploadName, removingRole, onFile, onBrowse, onRemove, onContinue, onBack, canContinue,
}: {
  slots: Record<Role, UploadSlot>;
  documents: Record<Role, DocumentSummary | undefined>;
  fileInputs: { current: Partial<Record<Role, HTMLInputElement | null>> };
  currentUploadName: string | null;
  removingRole: Role | null;
  onFile: (role: Role, file: File) => void;
  onBrowse: (role: Role) => void;
  onRemove: (role: Role, documentId: string) => void;
  onContinue: () => void;
  onBack: () => void;
  canContinue: boolean;
}) {
  return (
    <section className="upload-screen">
      <button type="button" className="back-link" onClick={onBack}><Icon name="arrowLeft" /> Back</button>
      <div className="section-intro">
        <span className="eyebrow">YOUR CLAIM, IN YOUR CONTROL</span>
        <h1>Upload your PDF documents</h1>
        <p>Please provide all four documents: Policy Wording, Policy Schedule, Hospital Bill, and Settlement Letter. All four are required to complete a thorough review.</p>
      </div>

      <div className="upload-grid">
        <article className="upload-card policy-upload-card">
          <div className="upload-card-head"><span className="document-icon violet"><Icon name="book" /></span><div><h2>Insurance policy</h2><p>Your cover, limits and policy wording</p></div><span className="required-pill">Both required</span></div>
          <p className="policy-note">You must upload both the policy wording and the policy schedule to continue.</p>
          <FileDrop role="policy_wording" label="Policy wording" detail="Benefits, terms and exclusions" slot={slots.policy_wording} selected={documents.policy_wording} inputRef={(element) => { fileInputs.current.policy_wording = element; }} onFile={onFile} onBrowse={onBrowse} onRemove={onRemove} removing={removingRole === 'policy_wording'} busy={currentUploadName !== null || removingRole !== null} />
          <FileDrop role="policy_schedule" label="Policy schedule" detail="Your plan, cover amount and limits" slot={slots.policy_schedule} selected={documents.policy_schedule} inputRef={(element) => { fileInputs.current.policy_schedule = element; }} onFile={onFile} onBrowse={onBrowse} onRemove={onRemove} removing={removingRole === 'policy_schedule'} busy={currentUploadName !== null || removingRole !== null} />
        </article>
        <article className="upload-card">
          <div className="upload-card-head"><span className="document-icon peach"><Icon name="receipt" /></span><div><h2>Hospital bill</h2><p>Itemized charges from your visit</p></div></div>
          <FileDrop role="bill" label="Add hospital bill" detail="A PDF of the itemized bill" slot={slots.bill} selected={documents.bill} inputRef={(element) => { fileInputs.current.bill = element; }} onFile={onFile} onBrowse={onBrowse} onRemove={onRemove} removing={removingRole === 'bill'} busy={currentUploadName !== null || removingRole !== null} />
        </article>
        <article className="upload-card">
          <div className="upload-card-head"><span className="document-icon blue"><Icon name="letter" /></span><div><h2>Settlement letter</h2><p>Payment details or reason for rejection</p></div></div>
          <FileDrop role="settlement" label="Add settlement letter" detail="The insurer’s decision or payment note" slot={slots.settlement} selected={documents.settlement} inputRef={(element) => { fileInputs.current.settlement = element; }} onFile={onFile} onBrowse={onBrowse} onRemove={onRemove} removing={removingRole === 'settlement'} busy={currentUploadName !== null || removingRole !== null} />
        </article>
      </div>

      <div className="upload-assurance"><span className="assurance-seal"><Icon name="lock" /></span><div><b>Your documents stay private.</b><span>They’re used only to prepare your claim review.</span></div><span className="pdf-note"><Icon name="file" /> PDF only · 25 MB · up to 250 pages</span></div>
      <div className="screen-actions"><span className="action-note">{canContinue ? 'Your documents are ready for checking.' : 'Add the policy wording, policy schedule, bill and settlement letter to continue.'}</span><button className="button button-primary" type="button" onClick={onContinue} disabled={!canContinue}>Continue <Icon name="arrowRight" /></button></div>
    </section>
  );
}

function FileDrop({
  role, label, detail, slot, selected, inputRef, onFile, onBrowse, onRemove, removing, busy,
}: {
  role: Role;
  label: string;
  detail: string;
  slot: UploadSlot;
  selected?: DocumentSummary;
  inputRef: (element: HTMLInputElement | null) => void;
  onFile: (role: Role, file: File) => void;
  onBrowse: (role: Role) => void;
  onRemove: (role: Role, documentId: string) => void;
  removing: boolean;
  busy: boolean;
}) {
  const [dragging, setDragging] = useState(false);
  const [confirmRemove, setConfirmRemove] = useState(false);
  const inputElement = useRef<HTMLInputElement | null>(null);
  const bindInput = (element: HTMLInputElement | null) => {
    inputElement.current = element;
    inputRef(element);
  };
  const currentFile = slot.status === 'error'
    ? selected?.original_filename ?? slot.fileName
    : slot.fileName ?? selected?.original_filename;
  const complete = slot.status === 'received' || (Boolean(selected) && slot.status !== 'uploading');
  const error = slot.status === 'error';
  const removableDocument = selected ?? slot.document;
  return (
    <div className={`file-drop-wrap ${complete ? 'file-complete' : ''} ${dragging ? 'is-dragging' : ''} ${error ? 'has-error' : ''}`}>
      <input ref={bindInput} className="visually-hidden" type="file" accept="application/pdf,.pdf" aria-label={`Choose ${label} PDF`} onChange={(event) => {
        const file = event.currentTarget.files?.[0];
        if (file) onFile(role, file);
        event.currentTarget.value = '';
      }} />
      {complete ? (
        <div className="file-complete-row">
          <span className="file-status-icon"><Icon name="check" /></span>
          <div className="file-main"><b>{label}</b><span title={currentFile}>{currentFile ?? 'Document received'}</span></div>
          <span className="file-complete-state">Uploaded</span>
          <div className="file-actions">
            <button className="quiet-button" type="button" onClick={() => onBrowse(role)} disabled={busy}>Replace</button>
            {removableDocument && removing && <span className="remove-pending"><span className="mini-spinner" /> Removing…</span>}
            {removableDocument && !removing && !confirmRemove && (
              <button className="quiet-button remove-file-button" type="button" onClick={() => setConfirmRemove(true)} disabled={busy}>Remove</button>
            )}
            {removableDocument && !removing && confirmRemove && (
              <span className="remove-confirmation">
                <span>Remove this PDF from your review?</span>
                <button className="quiet-button" type="button" onClick={() => setConfirmRemove(false)} disabled={busy}>Keep</button>
                <button className="quiet-button remove-file-button" type="button" onClick={() => { setConfirmRemove(false); onRemove(role, removableDocument.document_id); }} disabled={busy}>Remove PDF</button>
              </span>
            )}
          </div>
        </div>
      ) : slot.status === 'uploading' ? (
        <div className="file-upload-progress" aria-live="polite">
          <div className="upload-progress-icon"><Icon name="file" /></div>
          <div className="upload-progress-copy"><div><b title={currentFile}>{currentFile ?? label}</b><span>{slot.progress ?? 0}%</span></div><div className="progress-track" role="progressbar" aria-label={`Uploading ${label}`} aria-valuemin={0} aria-valuemax={100} aria-valuenow={slot.progress ?? 0}><span style={{ width: `${slot.progress ?? 0}%` }} /></div><small>Uploading securely</small></div>
        </div>
      ) : (
        <button
          className="file-drop-button"
          type="button"
          onClick={() => onBrowse(role)}
          onDragEnter={(event) => { event.preventDefault(); setDragging(true); }}
          onDragOver={(event) => event.preventDefault()}
          onDragLeave={() => setDragging(false)}
          onDrop={(event) => { event.preventDefault(); setDragging(false); const file = event.dataTransfer.files?.[0]; if (file) onFile(role, file); }}
          disabled={busy}
        >
          <span className="drop-icon"><Icon name="upload" /></span>
          <span className="drop-copy"><b>{label}</b><small>{detail}</small></span>
          <span className="drop-action">Choose PDF <Icon name="arrowUpRight" /></span>
        </button>
      )}
      {error && <p className="upload-error" role="alert"><Icon name="alert" />{slot.message ?? 'Please try uploading this document again.'}</p>}
      {slot.status === 'received' && !selected && <p className="upload-success">Uploaded. We’ll check this document next.</p>}
      {slot.status === 'removed' && <p className="upload-success" role="status">{slot.message}</p>}
    </div>
  );
}

function ProcessingScreen({
  status, documents, busy, slow, message, pageError, onRetry, onRetryDocument, onReplaceDocument, onAssignRole, onBack,
}: {
  status: ProcessingStatus | null;
  documents: DocumentSummary[];
  busy: boolean;
  slow: boolean;
  message: string | null;
  pageError: string | null;
  onRetry: () => void;
  onRetryDocument: (document: DocumentSummary) => void;
  onReplaceDocument: (document: DocumentSummary) => void;
  onAssignRole: (role: Role, documentId: string, confirm: boolean) => void;
  onBack: () => void;
}) {
  const activeCount = status?.active_jobs ?? 0;
  const selectedDocuments = status?.documents.filter((doc) => doc.selected_for_role) ?? [];
  const allSourcesSelected = ROLES.every((role) => selectedDocuments.some((doc) => doc.assigned_role === role));
  const readsSettled = selectedDocuments.length >= ROLES.length
    && selectedDocuments.every((doc) => doc.latest_run && !['QUEUED', 'RUNNING'].includes(doc.latest_run.status));
  
  const problemDocuments = selectedDocuments
    .filter((doc) => {
      const run = doc.latest_run;
      if (!run) return false;
      return run.status === 'FAILED' ||
             run.status === 'SUPERSEDED' ||
             (run.status === 'SUCCEEDED' && run.detected_role !== null && run.detected_role !== doc.assigned_role) ||
             (run.status === 'SUCCEEDED' && run.detected_role === null);
    })
    .map((item) => ({
      docSummary: documents.find((document) => document.document_id === item.document_id)!,
      procDoc: item
    }))
    .filter((x) => Boolean(x.docSummary));

  const hasProblem = problemDocuments.length > 0;
  const readingComplete = allSourcesSelected && readsSettled && activeCount === 0 && !hasProblem;
  const readingInProgress = activeCount > 0 && selectedDocuments.some((doc) => !doc.latest_run || ['QUEUED', 'RUNNING'].includes(doc.latest_run.status));
  const analysisInProgress = Boolean(message);
  const stalled = allSourcesSelected && !readsSettled && activeCount === 0 && !busy;
  const waiting = activeCount > 0 || busy;

  return (
    <section className="processing-screen">
      <div className="processing-art" aria-hidden="true">
        <div className="processing-orbit orbit-a" /><div className="processing-orbit orbit-b" />
        <div className="process-sun"><span /><span /><span /></div>
        <div className="process-document doc-one"><i /><i /><i /><b /></div>
        <div className="process-document doc-two"><i /><i /><i /><b /></div>
        <div className="process-check"><Icon name="spark" /></div>
      </div>
      <div className="processing-copy">
        <span className="eyebrow"><span className={`live-pulse ${stalled || hasProblem ? 'is-idle' : ''}`} /> {hasProblem ? 'A FILE NEEDS ATTENTION' : stalled ? 'WAITING FOR A CHECK' : 'YOUR REVIEW IS UNDER WAY'}</span>
        <h1>{message ? 'Putting your review together' : hasProblem ? 'One document needs attention' : stalled ? 'Your documents are waiting to be checked' : waiting ? 'Reading your documents' : 'Getting your review ready'}</h1>
        <p>{message ?? (hasProblem
          ? 'We found an issue with one of the documents you uploaded.'
          : stalled
            ? 'We haven’t received a processing update yet. Check again in a moment; we won’t mark any step complete until the files are checked.'
            : waiting
              ? 'We’re checking the files you shared. We’ll move on when those checks are complete.'
              : 'We’re gathering the document details so you can review them.')}</p>
        <div className="processing-checklist" aria-label="Review progress" aria-live="polite">
          <ProcessingRow label="Reading your documents" complete={readingComplete} active={!readingComplete && readingInProgress} />
          <ProcessingRow label="Finding relevant policy terms" complete={false} active={analysisInProgress} />
          <ProcessingRow label="Reconstructing the settlement" complete={false} active={analysisInProgress} />
          <ProcessingRow label="Checking deductions" complete={false} active={analysisInProgress} />
          <ProcessingRow label="Preparing your review" complete={false} active={analysisInProgress} />
        </div>
        <div className="processing-note"><Icon name="lock" /> These checks are part of the same review. We’ll only show a result when it’s ready.</div>
        
        {hasProblem && (
          <div className="failed-document-list">
            {problemDocuments.map(({ docSummary, procDoc }) => {
              const run = procDoc.latest_run!;
              const isFailed = run.status === 'FAILED';
              const isMismatch = run.status === 'SUCCEEDED' && run.detected_role !== null && run.detected_role !== procDoc.assigned_role;
              const isUnknown = run.status === 'SUCCEEDED' && run.detected_role === null;
              
              let problemMessage = "We couldn’t read this file. You can try it again.";
              if (isFailed) {
                problemMessage = (run.error_code && run.error_code !== "ANALYSIS_FAILED") ? run.error_code.replace(/_/g, ' ') : "We couldn't read this PDF.";
              } else if (isMismatch) {
                problemMessage = `This looks like a ${ROLE_COPY[run.detected_role!].title.toLowerCase()}, not a ${ROLE_COPY[procDoc.assigned_role].title.toLowerCase()}.`;
              } else if (isUnknown) {
                problemMessage = "We couldn't recognize this document as one of the documents needed for a claim review.";
              }

              return (
                <div className="failed-document-row" key={docSummary.document_id}>
                  <div>
                    <span className="eyebrow">{ROLE_COPY[procDoc.assigned_role].title.toUpperCase()}</span><br/>
                    <b>{docSummary.original_filename}</b>
                    <small>{problemMessage}</small>
                  </div>
                  <div className="failed-document-actions">
                    <button className="button button-secondary button-small" onClick={() => onReplaceDocument(docSummary)} type="button">Replace PDF</button>
                    {isMismatch && run.detected_role && (
                      <button className="button button-secondary button-small" onClick={() => onAssignRole(run.detected_role!, docSummary.document_id, true)} type="button">
                        Use it as {ROLE_COPY[run.detected_role!].title.toLowerCase()}
                      </button>
                    )}
                    {isFailed && (
                       <button className="button button-secondary button-small" onClick={() => onRetryDocument(docSummary)} type="button">Try again</button>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        )}

        {pageError && <div className="inline-error" role="alert"><span>{pageError}</span><button className="button button-secondary button-small" onClick={onRetry} type="button">Try again</button></div>}
        
        {slow && !hasProblem && (
          <div className="slow-note">
            <span className="mini-spinner" /> Still checking your documents...
            <ul className="slow-document-list">
              {selectedDocuments.map(doc => {
                const docSum = documents.find(d => d.document_id === doc.document_id);
                const runStatus = doc.latest_run?.status;
                const isTerminal = runStatus && !['QUEUED', 'RUNNING'].includes(runStatus);
                const statusText = isTerminal ? 'Done' : (runStatus === 'RUNNING' ? 'Processing...' : 'Waiting...');
                return (
                  <li key={doc.document_id}>
                    <b>{docSum?.original_filename}:</b> {statusText}
                  </li>
                );
              })}
            </ul>
          </div>
        )}

        {stalled && <button className="button button-secondary button-small" type="button" onClick={onRetry}>Check again <Icon name="refresh" /></button>}
        {!waiting && !stalled && !pageError && !hasProblem && <button className="text-button" type="button" onClick={onBack}><Icon name="arrowLeft" /> Back to documents</button>}
        {hasProblem && <button className="text-button" type="button" onClick={onBack}><Icon name="arrowLeft" /> Review documents</button>}
      </div>
    </section>
  );
}

function ProcessingRow({ label, complete, active }: { label: string; complete: boolean; active: boolean }) {
  return <div className={`processing-row ${complete ? 'complete' : ''} ${active ? 'active' : ''}`}><span className="processing-row-icon">{complete ? <Icon name="check" /> : active ? <span className="mini-spinner" /> : <span />}</span><span>{label}</span>{active && <small>In progress</small>}{complete && <small>Ready</small>}</div>;
}

function ReviewScreen({
  readiness, queue, documents, loading, pageError, analysisHistory, correctingItem, draftValues, cardMessages, verifiedPages,
  evidenceCache, onOpenEvidence, onCorrecting, onDraft, onSubmit, onAssign, onAddDocuments,
  onRetryDocument, onRefresh, onAnalyze,
}: {
  readiness: Readiness | null;
  queue: ReviewQueue | null;
  documents: DocumentSummary[];
  loading: boolean;
  pageError: string | null;
  analysisHistory: AnalysisRun[];
  correctingItem: string | null;
  draftValues: Record<string, string>;
  cardMessages: Record<string, string>;
  verifiedPages: Record<string, number>;
  evidenceCache: Record<string, EvidenceDetail>;
  onOpenEvidence: (item: ReviewItem | ReviewedSource) => void;
  onCorrecting: (itemId: string | null) => void;
  onDraft: (itemId: string, value: string) => void;
  onSubmit: (item: ReviewItem, action: ReviewAction) => void;
  onAssign: (role: Role, documentId: string, confirmMismatch?: boolean) => void;
  onAddDocuments: () => void;
  onRetryDocument: (document: DocumentSummary) => void;
  onRefresh: () => void;
  onAnalyze: () => void;
}) {
  const assignedRoles = readiness?.selected_roles ?? {} as Record<Role, string | null>;
  const missingRoles = ROLES.filter((role) => !assignedRoles[role]);
  const visibleItems = (queue?.items ?? []).filter((item) => item.field_path || !item.role || Boolean(assignedRoles[item.role]));
  const earlierRuns = readiness ? analysisHistory.filter((run) => run.status !== 'SUCCEEDED' || run.input_revision !== readiness.input_revision) : [];
  const statusTitle = readiness?.ready_for_analysis
    ? 'Your details are ready'
    : visibleItems.length === 1 ? 'We found something we want you to confirm.'
      : visibleItems.length > 1 ? 'We found a few details we’d like you to confirm.'
        : 'Let’s make sure we have the right documents.';
  return (
    <section className="review-screen">
      <div className="review-intro">
        <span className="eyebrow">A QUICK LOOK TOGETHER</span>
        <h1>{statusTitle}</h1>
        <p>Check the details below against your documents. Your review only moves forward when the important information is clear.</p>
      </div>

      {pageError && <div className="inline-error review-page-error" role="alert"><span>{pageError}</span><button className="button button-secondary button-small" type="button" onClick={onRefresh}>Refresh and try again</button></div>}
      {loading ? <ReviewSkeleton /> : (
        <>
          <div className="review-progress-band">
            <div className="review-progress-icon"><Icon name={readiness?.ready_for_analysis ? 'check' : 'eye'} /></div>
            <div><b>{readiness?.ready_for_analysis ? 'You’re all set for the review.' : missingRoles.length ? `Choose ${missingRoles.length} document source${missingRoles.length === 1 ? '' : 's'}` : `${visibleItems.length} detail${visibleItems.length === 1 ? '' : 's'} to check`}</b><span>{readiness?.ready_for_analysis ? 'Your documents are ready for a claim review.' : missingRoles.length ? 'Select a source for each document section before checking its details.' : 'You’re in control. Confirm, correct, or tell us when something isn’t clear.'}</span></div>
            <button className="icon-button subtle" type="button" onClick={onRefresh} aria-label="Refresh review details"><Icon name="refresh" /></button>
          </div>

          <ReviewHistory runs={earlierRuns} />

          {missingRoles.length > 0 && (
            <section className="source-setup-card">
              <div className="source-setup-heading"><div><span className="eyebrow">YOUR DOCUMENTS</span><h2>Choose the right source</h2><p>Each section should use the document that contains those details.</p></div><span className="source-count">{ROLES.length - missingRoles.length} of {ROLES.length} selected</span></div>
              <div className="role-choice-list">
                {missingRoles.map((role) => {
                  const candidates = documents.filter((doc) => doc.role === role);
                  return <RoleChoice key={role} role={role} candidates={candidates} onAssign={onAssign} onAddDocuments={onAddDocuments} />;
                })}
              </div>
            </section>
          )}

          {queue?.warnings.some((warning) => warning.code === 'POLICY_SCHEDULE_PARSER_UNAVAILABLE') && (
            <div className="gentle-note"><span><Icon name="info" /></span><p>Some policy schedules need a little extra care to read. We’ll ask you to confirm any details we can’t verify automatically.</p></div>
          )}

          {visibleItems.length ? (
            <div className="review-list">
              {visibleItems.map((item, index) => item.field_path ? (
                <ReviewCard
                  key={item.item_id}
                  item={item}
                  index={index}
                  evidence={item.candidate_evidence_id ? evidenceCache[item.candidate_evidence_id] : undefined}
                  correcting={correctingItem === item.item_id}
                  draft={draftValues[item.item_id]}
                  message={cardMessages[item.item_id] ?? ''}
                  verifiedPage={verifiedPages[item.item_id]}
                  onOpenEvidence={() => onOpenEvidence(item)}
                  onCorrecting={() => onCorrecting(correctingItem === item.item_id ? null : item.item_id)}
                  onDraft={(value) => onDraft(item.item_id, value)}
                  onSubmit={(action) => onSubmit(item, action)}
                />
              ) : (
                <ReviewIssueCard
                  key={item.item_id}
                  item={item}
                  documents={documents}
                  onAssign={onAssign}
                  onAddDocuments={onAddDocuments}
                  onRetryDocument={onRetryDocument}
                />
              ))}
            </div>
          ) : visibleItems.length === 0 && !readiness?.ready_for_analysis && missingRoles.length === 0 ? (
            <div className="review-empty-state"><div className="soft-loader"><span /><span /><span /></div><h3>We’re checking the details</h3><p>Refresh in a moment to see what may need your attention.</p><button className="button button-secondary" onClick={onRefresh} type="button">Refresh details <Icon name="refresh" /></button></div>
          ) : null}

          {readiness?.ready_for_analysis && visibleItems.length === 0 && (
            <div className="ready-card"><div className="ready-seal"><Icon name="check" /></div><div><span className="eyebrow">YOUR REVIEW IS READY</span><h2>We have what we need.</h2><p>Next, we’ll compare the settlement with the details you’ve confirmed.</p></div><button className="button button-primary" type="button" onClick={onAnalyze}>See my claim review <Icon name="arrowRight" /></button></div>
          )}

          {visibleItems.length === 0 && missingRoles.length === 0 && !readiness?.ready_for_analysis && (
            <div className="review-empty-state"><h3>One more detail is needed</h3><p>Make sure the required documents are selected, then refresh to continue.</p><button className="button button-secondary" onClick={onRefresh} type="button">Refresh details <Icon name="refresh" /></button></div>
          )}
        </>
      )}
    </section>
  );
}

function RoleChoice({ role, candidates, onAssign, onAddDocuments }: {
  role: Role; candidates: DocumentSummary[]; onAssign: (role: Role, documentId: string) => void; onAddDocuments: () => void;
}) {
  const [selected, setSelected] = useState(candidates[0]?.document_id ?? '');
  useEffect(() => setSelected(candidates[0]?.document_id ?? ''), [candidates]);
  return (
    <div className="role-choice-row">
      <span className="role-choice-icon"><Icon name={ROLE_COPY[role].icon} /></span>
      <div className="role-choice-label"><b>{ROLE_COPY[role].title}</b><small>{ROLE_COPY[role].short}</small></div>
      {candidates.length ? <>
        <select value={selected} onChange={(event) => setSelected(event.target.value)} aria-label={`Choose ${ROLE_COPY[role].title}`}>
          {candidates.map((document) => <option value={document.document_id} key={document.document_id}>{document.original_filename}</option>)}
        </select>
        <button className="button button-secondary button-small" onClick={() => selected && onAssign(role, selected)} type="button">Choose source</button>
      </> : <button className="text-button compact" onClick={onAddDocuments} type="button">Add document <Icon name="arrowRight" /></button>}
    </div>
  );
}

function ReviewIssueCard({
  item, documents, onAssign, onAddDocuments, onRetryDocument,
}: {
  item: ReviewItem;
  documents: DocumentSummary[];
  onAssign: (role: Role, documentId: string, confirmMismatch?: boolean) => void;
  onAddDocuments: () => void;
  onRetryDocument: (document: DocumentSummary) => void;
}) {
  const role = item.role;
  const source = item.document_id ? documents.find((document) => document.document_id === item.document_id) : undefined;
  const mismatchNeedsConsent = item.code === 'ROLE_MISMATCH_REQUIRES_EXPLICIT_RESOLUTION' || item.code === 'PROCESSING_ROLE_MISMATCH';
  const canRetry = item.code === 'PROCESSING_RUN_NOT_USABLE' && source;
  const alternatives = role ? documents.filter((document) => document.role === role && document.document_id !== item.document_id) : [];
  const heading = mismatchNeedsConsent
    ? `Check this ${displayRole(role).toLowerCase()} source`
    : canRetry ? `Check this ${displayRole(role).toLowerCase()} again`
      : role ? `Choose a ${displayRole(role).toLowerCase()} source` : 'We need another document';
  const description = mismatchNeedsConsent
    ? 'This file may belong to a different section. If it is still the right source, you can confirm it below.'
    : canRetry ? 'We couldn’t finish checking this file. You can ask us to try it again.'
      : item.code === 'PROCESSING_SOURCE_HASH_MISMATCH' || item.code === 'ROLE_ASSIGNMENT_SOURCE_HASH_MISMATCH'
        ? 'This file changed after it was checked. Upload the current copy so we can review it safely.'
        : role ? `We couldn’t verify the ${displayRole(role).toLowerCase()} details from the selected file. Choose another source or add a clearer copy.`
          : 'We couldn’t verify this part from the current files. Add a clearer copy so you can continue.';
  return (
    <article className="review-issue-card">
      <div className="review-issue-marker"><Icon name="info" /></div>
      <div className="review-issue-main">
        <div className="review-card-top"><span className="review-category">{displayRole(role)}</span><span className="issue-badge">NEEDS A LOOK</span></div>
        <h2>{heading}</h2>
        <p>{description}</p>
        {source && <div className="review-issue-file"><Icon name="file" /> <span>{source.original_filename}</span></div>}
        <div className="review-issue-actions">
          {mismatchNeedsConsent && role && source && <button className="button button-primary button-small" type="button" onClick={() => onAssign(role, source.document_id, true)}>Yes, use this source <Icon name="check" /></button>}
          {canRetry && source && <button className="button button-secondary button-small" type="button" onClick={() => onRetryDocument(source)}>Try this file again <Icon name="refresh" /></button>}
          {role && alternatives.length > 0 && <RoleChoice role={role} candidates={alternatives} onAssign={onAssign} onAddDocuments={onAddDocuments} />}
          {!mismatchNeedsConsent && !canRetry && alternatives.length === 0 && <button className="button button-secondary button-small" type="button" onClick={onAddDocuments}>Add or replace a document <Icon name="arrowRight" /></button>}
          {mismatchNeedsConsent && <button className="text-button compact" type="button" onClick={onAddDocuments}>Choose a different file <Icon name="arrowRight" /></button>}
        </div>
      </div>
    </article>
  );
}

function ReviewCard({
  item, index, evidence, correcting, draft, message, verifiedPage, onOpenEvidence, onCorrecting, onDraft, onSubmit,
}: {
  item: ReviewItem;
  index: number;
  evidence?: EvidenceDetail;
  correcting: boolean;
  draft?: string;
  message: string;
  verifiedPage?: number;
  onOpenEvidence: () => void;
  onCorrecting: () => void;
  onDraft: (value: string) => void;
  onSubmit: (action: ReviewAction) => void;
}) {
  const kind = fieldKind(item.field_path);
  const label = humanField(item.field_path);
  const hasCandidate = item.candidate_value !== null && item.candidate_value !== undefined;
  const visualRequired = requiresVisual(item.field_path) || !item.candidate_evidence_id;
  const initialDraft = kind === 'money' && typeof item.candidate_value === 'number'
    ? (item.candidate_value / 100).toFixed(2)
    : item.candidate_value === null || item.candidate_value === undefined ? '' : String(item.candidate_value);
  const correctionValue = draft ?? initialDraft;
  return (
    <article className="review-card">
      <div className="review-card-marker">{String(index + 1).padStart(2, '0')}</div>
      <div className="review-card-main">
        <div className="review-card-top"><span className="review-category">{displayRole(item.role)}</span><span className={`candidate-badge ${item.candidate_state === 'VERIFIED' ? 'candidate-found' : 'candidate-review'}`}>{candidateState(item.candidate_state)}</span></div>
        <h2>{label}</h2>
        <div className="review-value-row"><div><small>{hasCandidate ? 'Found in your document' : 'Needs your attention'}</small><strong>{formatCandidate(item)}</strong></div>{hasCandidate && <span className="value-check"><Icon name="spark" /> Check this detail</span>}</div>
        <p className="review-description">{reviewDescription(item, hasCandidate)}</p>
        <div className="review-evidence-line">
          <div className="evidence-snippet-mark"><Icon name="quote" /></div>
          <div className="evidence-snippet-copy"><b>{evidence ? `${displayRole(evidence.document_role)} · Page ${evidence.page_number}` : 'Source document'}</b><span>{evidence?.quoted_text ? `“${truncate(evidence.quoted_text, 116)}”` : 'Open the original page to check this detail.'}</span></div>
          <button type="button" className="text-button compact" onClick={onOpenEvidence}>View source <Icon name="arrowUpRight" /></button>
        </div>

        {correcting && <CorrectionEditor item={item} value={correctionValue} verifiedPage={verifiedPage} onChange={onDraft} onOpenEvidence={onOpenEvidence} onSave={() => onSubmit('CORRECT')} />}
        {message && <div className={`review-feedback ${message.startsWith('Saved') || message.startsWith('We’ve saved') ? 'feedback-success' : ''}`} role="status"><Icon name={message.startsWith('Saved') || message.startsWith('We’ve saved') ? 'check' : 'info'} />{message}</div>}
        <div className="review-card-actions">
          <button className="button button-primary button-confirm" type="button" onClick={() => onSubmit('CONFIRM')} disabled={!hasCandidate || (visualRequired && !verifiedPage)}><Icon name="check" /> Confirm</button>
          <button className="button button-secondary" type="button" onClick={onCorrecting}>Correct</button>
          <button className="quiet-button unresolved-button" type="button" onClick={() => onSubmit('UNRESOLVED')}>Can’t verify</button>
          {visualRequired && !verifiedPage && <span className="review-action-hint">Open the source before confirming.</span>}
        </div>
      </div>
    </article>
  );
}

function CorrectionEditor({ item, value, verifiedPage, onChange, onOpenEvidence, onSave }: {
  item: ReviewItem;
  value: string;
  verifiedPage?: number;
  onChange: (value: string) => void;
  onOpenEvidence: () => void;
  onSave: () => void;
}) {
  const kind = fieldKind(item.field_path);
  return (
    <div className="correction-editor">
      <div className="correction-editor-heading"><span className="eyebrow">MAKE A CORRECTION</span><span>Check the source page before saving.</span></div>
      {kind === 'boolean' ? <select value={value} onChange={(event) => onChange(event.target.value)} aria-label="Corrected yes or no value"><option value="">Choose yes or no</option><option value="true">Yes</option><option value="false">No</option></select>
        : kind === 'category' ? <select value={value} onChange={(event) => onChange(event.target.value)} aria-label="Corrected charge type"><option value="">Choose a charge type</option>{CATEGORY_OPTIONS.map((option) => <option value={option} key={option}>{humanize(option)}</option>)}</select>
        : <label className="correction-input-wrap"><span>{kind === 'money' ? 'Correct amount (₹)' : kind === 'percent' ? 'Correct percentage' : kind === 'date' ? 'Correct date' : 'Correct detail'}</span><input type={kind === 'date' ? 'date' : 'text'} inputMode={kind === 'money' || kind === 'percent' ? 'decimal' : 'text'} value={value} onChange={(event) => onChange(event.target.value)} placeholder={kind === 'money' ? 'e.g. 1250.00' : kind === 'percent' ? 'e.g. 10' : 'Enter what you see'} /></label>}
      <div className="correction-proof-row"><span className={verifiedPage ? 'proof-confirmed' : ''}><Icon name={verifiedPage ? 'check' : 'info'} />{verifiedPage ? `Page ${verifiedPage} checked` : 'Visual confirmation is needed for a correction.'}</span><button className="text-button compact" type="button" onClick={onOpenEvidence}>Review source page <Icon name="arrowUpRight" /></button></div>
      <button className="button button-primary button-small" type="button" onClick={onSave} disabled={!verifiedPage}>Save correction <Icon name="arrowRight" /></button>
    </div>
  );
}

function candidateState(state: string | null): string {
  if (state === 'VERIFIED') return 'Source checked';
  if (state === 'PROPOSED') return 'Needs a quick check';
  if (state === 'CONFLICTING') return 'Details differ';
  if (state === 'UNREADABLE') return 'Hard to read';
  if (state === 'QUARANTINED') return 'Not used yet';
  return 'Needs a quick check';
}

function reviewDescription(item: ReviewItem, hasCandidate: boolean): string {
  if (item.latest_correction_action === 'UNRESOLVED') return 'You marked this as unclear. You can check the page again, enter a correction, or leave it unresolved.';
  if (!hasCandidate) {
    if (item.reason === 'PARSER_NOT_AVAILABLE' || item.candidate_state === 'UNREADABLE') {
      return 'We couldn’t reliably read this detail from the document. If you can see it, enter what it says; otherwise choose “Can’t verify”.';
    }
    if (item.reason === 'AMBIGUOUS' || item.candidate_state === 'QUARANTINED' || item.candidate_state === 'CONFLICTING') {
      return 'We found this detail, but we’re not confident how it should be classified. If you can see it, enter what it says; otherwise choose “Can’t verify”.';
    }
    return 'This detail does not appear to be present in the uploaded document. If you can see it, enter what it says; otherwise choose “Can’t verify”.';
  }
  return 'Take a moment to compare this detail with the page shown in your document.';
}

function truncate(value: string, maximum: number): string {
  const compact = value.replace(/\s+/g, ' ').trim();
  return compact.length > maximum ? `${compact.slice(0, maximum).trim()}…` : compact;
}

function ReviewSkeleton() {
  return <div className="review-skeleton"><span /><span /><span /></div>;
}

function ReviewHistory({ runs }: { runs: AnalysisRun[] }) {
  if (!runs.length) return null;
  return (
    <section className="review-history" aria-label="Earlier reviews">
      <div className="review-history-intro"><span className="eyebrow">FOR YOUR RECORDS</span><h2>Earlier reviews</h2><p>These saved attempts stay separate from the review of your current documents.</p></div>
      <ul>{runs.map((run) => <li key={run.analysis_run_id}><span className={`history-status history-${run.status.toLowerCase()}`}>{historyStatus(run.status)}</span><time dateTime={run.completed_at ?? run.created_at}>{formatHistoryDate(run.completed_at ?? run.created_at)}</time></li>)}</ul>
    </section>
  );
}

function historyStatus(status: AnalysisRun['status']): string {
  if (status === 'SUCCEEDED') return 'Review saved';
  if (status === 'FAILED') return 'Couldn’t finish';
  if (status === 'QUEUED' || status === 'RUNNING') return 'In progress';
  return 'Stopped';
}

function formatHistoryDate(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return 'Saved earlier';
  return new Intl.DateTimeFormat('en-IN', { dateStyle: 'medium', timeStyle: 'short' }).format(date);
}

function EvidenceDrawer({
  caseId, document, evidence, initialPage, onClose, onUse,
}: {
  caseId: string;
  document: DocumentSummary;
  evidence?: EvidenceDetail;
  initialPage: number;
  onClose: () => void;
  onUse: (page: number, evidence?: EvidenceDetail) => void;
}) {
  const [page, setPage] = useState(initialPage);
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);
  const [previewError, setPreviewError] = useState(false);
  const [loadingPreview, setLoadingPreview] = useState(true);
  const [previewAttempt, setPreviewAttempt] = useState(0);
  useEffect(() => setPage(initialPage), [initialPage]);
  useEffect(() => {
    let cancelled = false;
    let objectUrl: string | null = null;
    setLoadingPreview(true);
    setPreviewError(false);
    setPreviewUrl(null);
    fetch(`${API}/cases/${encodeURIComponent(caseId)}/documents/${encodeURIComponent(document.document_id)}/content`, { credentials: 'same-origin' })
      .then((response) => {
        if (!response.ok) throw new Error('Unavailable');
        return response.blob();
      })
      .then((blob) => {
        if (cancelled) return;
        objectUrl = URL.createObjectURL(blob);
        setPreviewUrl(objectUrl);
      })
      .catch(() => { if (!cancelled) setPreviewError(true); })
      .finally(() => { if (!cancelled) setLoadingPreview(false); });
    return () => {
      cancelled = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [caseId, document.document_id, previewAttempt]);
  useEffect(() => {
    const previousOverflow = window.document.body.style.overflow;
    window.document.body.style.overflow = 'hidden';
    return () => { window.document.body.style.overflow = previousOverflow; };
  }, []);
  useEffect(() => {
    const closeOnEscape = (event: KeyboardEvent) => { if (event.key === 'Escape') onClose(); };
    window.addEventListener('keydown', closeOnEscape);
    return () => window.removeEventListener('keydown', closeOnEscape);
  }, [onClose]);
  const pdfUrl = previewUrl ? `${previewUrl}#page=${page}&toolbar=0&navpanes=0` : undefined;
  const pageEvidence = evidence?.page_number === page ? evidence : undefined;
  return (
    <div className="drawer-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
      <section className="evidence-drawer" role="dialog" aria-modal="true" aria-labelledby="evidence-title">
        <header className="drawer-header"><div><span className="eyebrow">YOUR SOURCE DOCUMENT</span><h2 id="evidence-title">{displayRole(document.role)}</h2><p>{document.original_filename}</p></div><button className="icon-button" type="button" onClick={onClose} aria-label="Close source view"><Icon name="close" /></button></header>
        <div className="drawer-content">
          <div className="document-preview-column">
            <div className="preview-toolbar"><span><Icon name="file" /> PDF preview</span><label>Page <input type="number" min={1} max={document.page_count} value={page} onChange={(event) => setPage(Math.min(document.page_count, Math.max(1, Number(event.target.value) || 1)))} /> <span>of {document.page_count}</span></label></div>
            <div className="pdf-frame-wrap">
              {loadingPreview && <div className="pdf-loading" role="status"><span className="mini-spinner" /> Opening your document…</div>}
              {previewError && <div className="pdf-error" role="alert"><Icon name="file" /><b>We couldn’t open the preview.</b><span>Your document is still attached to the review. Try again when you’re ready.</span><button className="button button-secondary button-small" type="button" onClick={() => setPreviewAttempt((attempt) => attempt + 1)}><Icon name="refresh" /> Try again</button></div>}
              {pdfUrl && <iframe title={`${displayRole(document.role)} page ${page}`} src={pdfUrl} className="pdf-frame" />}
            </div>
          </div>
          <aside className="source-detail-column">
            <div className="source-meta-pill"><span className="source-dot" /> {displayRole(document.role)} <i /> Page {page}</div>
            {pageEvidence?.quoted_text ? <div className="quote-card"><span className="quote-mark">“</span><span className="quote-label">TEXT FOUND ON THIS PAGE</span><blockquote>{pageEvidence.quoted_text}</blockquote><small>From the document you uploaded</small></div> : <div className="quote-card quote-empty"><span className="quote-mark">“</span><span className="quote-label">CHECK THE ORIGINAL PAGE</span><p>Compare the detail with the page shown on the left. We won’t add a quote unless it can be verified from your document.</p></div>}
            <div className="source-check-note"><Icon name="shield" /><p>Only you can confirm whether this page supports the detail.</p></div>
            <button className="button button-primary drawer-use-button" type="button" onClick={() => onUse(page, pageEvidence)} disabled={!previewUrl}>
              <Icon name="check" /> I checked this page
            </button>
            {!previewUrl && !loadingPreview && <small className="drawer-hint">Page confirmation is unavailable while the preview can’t be opened.</small>}
          </aside>
        </div>
      </section>
    </div>
  );
}

function ResultsScreen({
  isExampleCase, result, runStatus, currentRun, history, sources, documents, onOpenSource, onNewReview,
}: {
  isExampleCase: boolean;
  result: AnalysisResult | null;
  runStatus: AnalysisRun['status'];
  currentRun: AnalysisRun;
  history: AnalysisRun[];
  sources: ReviewedSource[];
  documents: DocumentSummary[];
  onOpenSource: (source: ReviewedSource) => void;
  onNewReview: () => void;
}) {
  if (runStatus === 'FAILED' || !result) return <AnalysisFailure onRetry={onNewReview} onReview={onNewReview} />;
  const overall = verdictCopy(result.verdict.state);
  const earlierRuns = history.filter((run) => run.analysis_run_id !== currentRun.analysis_run_id);
  const currentDocuments = documents.filter((document) => document.selected_for_role);
  return (
    <section className="results-screen">
      {isExampleCase && (
        <div className="example-claim-banner" role="note">
          <span className="example-claim-mark"><Icon name="info" /></span>
          <div><b>Example claim — illustrative only</b><p>This is not a real customer’s claim or outcome.</p></div>
        </div>
      )}
      <div className="results-heading">
        <span className="eyebrow"><span className="result-spark" /> YOUR CLAIM REVIEW</span>
        <h1>Your claim review is ready.</h1>
        <p>Here’s what the documents show, what may need clarification, and where you can look next.</p>
      </div>

      <section className={`verdict-card verdict-${overall.tone}`}>
        <div className="verdict-icon"><Icon name={overall.tone === 'supported' ? 'check' : overall.tone === 'question' ? 'spark' : 'info'} /></div>
        <div className="verdict-content"><span className="eyebrow">YOUR REVIEW SUMMARY</span><h2>{overall.title}</h2><p>{overall.body}</p></div>
        <span className="verdict-tag">{stateLabel(result.verdict.state)}</span>
      </section>

      <section className="money-overview" aria-label="Claim amounts">
        <div className="money-overview-top"><div><span className="eyebrow">THE BIG PICTURE</span><h2>What happened to the amounts?</h2></div><span className="money-overview-mark"><Icon name="chart" /></span></div>
        <div className="money-grid">
          <MoneyStat label="Hospital bill" amount={result.calculation.gross_bill_paise} detail="Total submitted" tone="neutral" />
          <MoneyStat label="Insurer paid" amount={result.reconciliation.paid_paise} detail="Shown in the settlement" tone="green" />
          <MoneyStat label="Potentially unexplained" amount={result.reconciliation.unexplained_paise} detail="May be worth asking about" tone="amber" emphasis />
        </div>
        <div className="money-footnote"><Icon name="info" /> A difference is a prompt to ask a question, not a conclusion that anyone did something wrong.</div>
      </section>

      <section className="findings-section">
        <div className="results-section-heading"><div><span className="eyebrow">DETAILS TO TAKE WITH YOU</span><h2>What stood out</h2></div><span className="section-counter">{result.findings.length} {result.findings.length === 1 ? 'item' : 'items'}</span></div>
        {result.findings.length ? <div className="finding-grid">
          {result.findings.map((finding, index) => <FindingCard finding={finding} index={index} key={finding.finding_id} />)}
        </div> : <div className="findings-empty"><span><Icon name="check" /></span><div><b>No additional findings to show</b><p>Keep your policy, bill and settlement together for your records.</p></div></div>}
      </section>

      <section className="sources-section">
        <div className="results-section-heading"><div><span className="eyebrow">YOUR DOCUMENTS</span><h2>{sources.length ? 'Pages you checked' : 'Your source documents'}</h2><p>{sources.length ? 'Open the original page or review the text we found there.' : 'Open a source document to check its original pages.'}</p></div><span className="source-count">{sources.length ? `${sources.length} pages` : `${currentDocuments.length} documents`}</span></div>
        {sources.length ? <div className="source-list">
          {sources.map((source) => <article className="result-source" key={source.key}>
            <span className="result-source-icon"><Icon name={source.role === 'bill' ? 'receipt' : source.role === 'settlement' ? 'letter' : 'book'} /></span>
            <div className="result-source-main"><b>{displayRole(source.role)} <span>· Page {source.pageNumber}</span></b><small>{source.fieldLabel}</small>{source.quote && <p>“{truncate(source.quote, 180)}”</p>}</div>
            <button className="text-button compact" type="button" onClick={() => onOpenSource(source)}>View page <Icon name="arrowUpRight" /></button>
          </article>)}
        </div> : <div className="source-list">
          {currentDocuments.map((document) => <article className="result-source" key={document.document_id}>
            <span className="result-source-icon"><Icon name={document.role === 'bill' ? 'receipt' : document.role === 'settlement' ? 'letter' : 'book'} /></span>
            <div className="result-source-main"><b>{displayRole(document.role)}</b><small>{document.original_filename}</small></div>
            <button className="text-button compact" type="button" onClick={() => onOpenSource({ key: document.document_id, documentId: document.document_id, role: document.role, pageNumber: 1, evidenceId: null, fieldLabel: displayRole(document.role), quote: null })}>View document <Icon name="arrowUpRight" /></button>
          </article>)}
        </div>}
        <div className="evidence-note"><Icon name="shield" /><span>Every page shown here comes from a document you uploaded. CLAIMCHECK does not invent policy wording or evidence.</span></div>
      </section>

      <section className="next-steps-section">
        <div className="next-steps-heading"><span className="eyebrow">WHAT YOU CAN DO NEXT</span><h2>Take the next step at your pace.</h2><p>These are practical questions you may choose to ask. They are not legal advice.</p></div>
        <div className="next-step-list">
          {result.findings.slice(0, 3).map((finding, index) => <NextStepCard finding={finding} index={index} key={finding.finding_id} />)}
          {result.findings.length === 0 && <div className="next-step-card"><span className="next-step-number">01</span><div><b>Keep a copy of your review</b><p>Save the policy and settlement pages alongside your claim documents.</p></div><Icon name="arrowRight" /></div>}
        </div>
      </section>

      <ReviewHistory runs={earlierRuns} />

      <section className="results-outro"><div className="outro-mark"><Brand /></div><div><h2>You’re in control of what happens next.</h2><p>Use this review to help you ask clearer questions about your claim.</p></div><button className="button button-secondary" type="button" onClick={onNewReview}>Review another claim <Icon name="arrowRight" /></button></section>
      <p className="results-disclaimer">CLAIMCHECK helps you understand the documents you shared. It does not decide your claim, provide legal advice, or replace your insurer’s formal explanation.</p>
    </section>
  );
}

function MoneyStat({ label, amount, detail, tone, emphasis = false }: { label: string; amount: number; detail: string; tone: string; emphasis?: boolean }) {
  return <div className={`money-stat money-${tone} ${emphasis ? 'money-emphasis' : ''}`}><span>{label}</span><AnimatedMoney value={amount} /><small>{detail}</small></div>;
}

function AnimatedMoney({ value }: { value: number }) {
  const [displayed, setDisplayed] = useState(0);
  useEffect(() => {
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
      setDisplayed(value);
      return;
    }
    let frame = 0;
    const startTime = performance.now();
    const duration = 800;
    const tick = (now: number) => {
      const progress = Math.min(1, (now - startTime) / duration);
      const eased = 1 - Math.pow(1 - progress, 3);
      setDisplayed(Math.round(value * eased));
      if (progress < 1) frame = requestAnimationFrame(tick);
    };
    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  }, [value]);
  return <strong>{formatMoney(displayed)}</strong>;
}

function FindingCard({ finding, index }: { finding: AnalysisFinding; index: number }) {
  const title = findingTitle(finding.head);
  return (
    <article className="finding-result-card">
      <div className="finding-result-top"><span className="finding-index">{String(index + 1).padStart(2, '0')}</span><span className={`finding-state state-${finding.state.toLowerCase()}`}>{stateLabel(finding.state)}</span></div>
      <div className="finding-result-body"><span className="eyebrow">{finding.type === 'EVIDENCE_GAP' ? 'DETAIL TO CHECK' : finding.type === 'PROCEDURAL' ? 'SETTLEMENT DETAIL' : finding.type === 'INFO' ? 'FOR YOUR RECORDS' : 'AMOUNT REVIEW'}</span><h3>{title}</h3>{finding.amount_paise !== null && <strong className="finding-result-amount">{formatMoney(finding.amount_paise)}</strong>}<p>{findingDescription(finding)}</p></div>
      <div className="finding-why"><span className="why-icon"><Icon name="spark" /></span><div><b>Why this matters</b><p>{finding.type === 'EVIDENCE_GAP' ? 'This part may affect what can be checked from the documents.' : 'This can affect how the final payable amount is understood.'}</p></div></div>
      <div className="finding-next"><b>A possible next step</b><p>{nextStep(finding)}</p></div>
    </article>
  );
}

function NextStepCard({ finding, index }: { finding: AnalysisFinding; index: number }) {
  const title = findingTitle(finding.head);
  const question = finding.state === 'POTENTIALLY_INCONSISTENT'
    ? `Could you explain how ${title.toLowerCase()} was applied to my claim, and which policy term was used?`
    : `Could you help me understand the ${title.toLowerCase()} detail in my claim review?`;
  const [copied, setCopied] = useState(false);
  const copyQuestion = async () => {
    try {
      await navigator.clipboard.writeText(question);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1800);
    } catch { setCopied(false); }
  };
  return <article className="next-step-card"><span className="next-step-number">{String(index + 1).padStart(2, '0')}</span><div className="next-step-content"><b>{title}</b><p>{nextStep(finding)}</p><button type="button" className="copy-question" onClick={() => void copyQuestion()}><Icon name={copied ? 'check' : 'copy'} />{copied ? 'Copied' : 'Copy a question to ask'}</button></div><Icon name="arrowRight" /></article>;
}

function AnalysisFailure({ onRetry, onReview }: { onRetry: () => void; onReview: () => void }) {
  return <section className="analysis-failure"><span className="failure-art"><Icon name="file" /></span><span className="eyebrow">YOUR DOCUMENTS ARE SAFE</span><h1>We couldn’t finish this review.</h1><p>Please try again. If a detail needs another look, you can return to your document review.</p><div><button className="button button-primary" type="button" onClick={onRetry}>Try again <Icon name="refresh" /></button><button className="button button-secondary" type="button" onClick={onReview}>Back to my review</button></div></section>;
}

function Icon({ name }: { name: string }) {
  const common = { width: 20, height: 20, viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', strokeWidth: 1.7, strokeLinecap: 'round' as const, strokeLinejoin: 'round' as const, 'aria-hidden': true as const };
  const paths: Record<string, ReactNode> = {
    lock: <><rect x="4.5" y="10" width="15" height="10" rx="2" /><path d="M8 10V7a4 4 0 0 1 8 0v3" /><path d="M12 14v2" /></>,
    arrowRight: <><path d="M4 12h15" /><path d="m13 6 6 6-6 6" /></>,
    arrowLeft: <><path d="M20 12H5" /><path d="m11 18-6-6 6-6" /></>,
    arrowUpRight: <><path d="M7 17 17 7" /><path d="M8 7h9v9" /></>,
    check: <path d="m5 12 4 4L19 6" />,
    close: <><path d="m6 6 12 12" /><path d="M18 6 6 18" /></>,
    spark: <><path d="m12 3 1.7 5.3L19 10l-5.3 1.7L12 17l-1.7-5.3L5 10l5.3-1.7L12 3Z" /><path d="m19 16 .8 2.2L22 19l-2.2.8L19 22l-.8-2.2L16 19l2.2-.8L19 16Z" /></>,
    info: <><circle cx="12" cy="12" r="9" /><path d="M12 11v5" /><path d="M12 8h.01" /></>,
    alert: <><path d="M12 3 2.8 19h18.4L12 3Z" /><path d="M12 9v4" /><path d="M12 16h.01" /></>,
    book: <><path d="M4 5.5A2.5 2.5 0 0 1 6.5 3H20v17H6.5A2.5 2.5 0 0 0 4 22V5.5Z" /><path d="M4 5.5V22" /><path d="M8 7h8M8 11h7" /></>,
    shield: <><path d="M12 3 20 6v5c0 5-3.4 8.1-8 10-4.6-1.9-8-5-8-10V6l8-3Z" /><path d="m9 12 2 2 4-4" /></>,
    receipt: <><path d="M6 3h12v18l-3-2-3 2-3-2-3 2V3Z" /><path d="M9 8h6M9 12h6M9 16h3" /></>,
    letter: <><rect x="3" y="5" width="18" height="14" rx="2" /><path d="m4 7 8 6 8-6" /><path d="M8 16h4" /></>,
    file: <><path d="M6 3h8l5 5v13H6a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2Z" /><path d="M14 3v6h5M8 13h8M8 17h6" /></>,
    files: <><path d="M5 7V4a1 1 0 0 1 1-1h8l4 4v3" /><path d="M14 3v5h4" /><rect x="4" y="9" width="15" height="12" rx="2" /><path d="M8 14h7M8 17h5" /></>,
    upload: <><path d="M12 16V4" /><path d="m7 9 5-5 5 5" /><path d="M5 14v5a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-5" /></>,
    eye: <><path d="M2.5 12s3.3-6 9.5-6 9.5 6 9.5 6-3.3 6-9.5 6-9.5-6-9.5-6Z" /><circle cx="12" cy="12" r="2.5" /></>,
    scan: <><path d="M4 8V5a1 1 0 0 1 1-1h3M16 4h3a1 1 0 0 1 1 1v3M20 16v3a1 1 0 0 1-1 1h-3M8 20H5a1 1 0 0 1-1-1v-3" /><path d="M7 12h10" /></>,
    sun: <><circle cx="12" cy="12" r="4" /><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4m11.4 11.4 1.4 1.4M2 12h2m16 0h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" /></>,
    quote: <><path d="M9 11H5a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h4v7Zm12 0h-4a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h4v7ZM3 11v1a6 6 0 0 0 6 6M15 11v1a6 6 0 0 0 6 6" /></>,
    refresh: <><path d="M20 7v5h-5" /><path d="M4 17v-5h5" /><path d="M5.5 9a7 7 0 0 1 12-2L20 12M4 12l2.5 5a7 7 0 0 0 12-2" /></>,
    fileCheck: <><path d="M6 3h8l5 5v13H6a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2Z" /><path d="M14 3v6h5M8 15l2 2 4-4" /></>,
    chart: <><path d="M4 19V5M4 19h16" /><path d="m7 15 4-4 3 2 5-6" /><path d="M16 7h3v3" /></>,
    copy: <><rect x="8" y="8" width="12" height="12" rx="2" /><path d="M16 8V5a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h3" /></>,
  };
  return <svg {...common}>{paths[name] ?? paths.spark}</svg>;
}

export default App;
