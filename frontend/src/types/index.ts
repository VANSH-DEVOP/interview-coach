/** API contracts - mirrors backend Pydantic schemas. */

export interface User {
  id: string;
  email: string;
  full_name: string;
  is_active: boolean;
  created_at: string;
}

export interface TokenPair {
  access_token: string;
  refresh_token: string;
  token_type: "bearer";
}

export type ResumeStatus = "uploaded" | "parsed" | "failed";

export interface Resume {
  id: string;
  file_name: string;
  content_type: string;
  size_bytes: number;
  status: ResumeStatus;
  created_at: string;
}

export type SessionStatus = "created" | "in_progress" | "completed" | "abandoned";
export type QuestionType = "behavioral" | "technical" | "follow_up";
export type InterviewType = "behavioral" | "technical" | "system_design" | "mixed";
export type DifficultyLevel = "junior" | "mid" | "senior";

export const MIN_QUESTION_COUNT = 3;
export const MAX_QUESTION_COUNT = 10;
export const DEFAULT_QUESTION_COUNT = 5;

export interface Answer {
  id: string;
  content: string;
  duration_seconds: number | null;
  created_at: string;
}

export interface Question {
  id: string;
  parent_question_id: string | null;
  sequence_number: number;
  content: string;
  question_type: QuestionType;
  answer: Answer | null;
}

export interface InterviewSession {
  id: string;
  title: string;
  target_role: string | null;
  resume_id: string | null;
  status: SessionStatus;
  interview_type: InterviewType;
  difficulty: DifficultyLevel;
  question_count: number;
  started_at: string | null;
  completed_at: string | null;
  created_at: string;
}

export interface InterviewSessionDetail extends InterviewSession {
  questions: Question[];
}

export type ReportStatus = "pending" | "generating" | "completed" | "failed";

export interface PerQuestionFeedback {
  question: string;
  answered?: boolean;
  feedback: string;
}

export interface ReportFeedback {
  summary?: string;
  recommendations?: string[];
  per_question?: PerQuestionFeedback[];
}

export interface EvaluationReport {
  id: string;
  session_id: string;
  overall_score: string | null;
  strengths: string[] | null;
  weaknesses: string[] | null;
  detailed_feedback: ReportFeedback | null;
  status: ReportStatus;
  created_at: string;
}

export interface Page<T> {
  items: T[];
  total: number;
  page: number;
  size: number;
}

export interface ApiErrorBody {
  error: {
    code: string;
    message: string;
    details?: unknown;
  };
}
