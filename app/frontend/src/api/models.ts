export type ChatAppRequestOverrides = {
    reasoning_effort?: string;
    seed?: number;
    temperature?: number;
    suggest_followup_questions?: boolean;
    language: string;
};

export type ResponseMessage = {
    content: string;
    role: string;
};

export type Thoughts = {
    title: string;
    description: any; // It can be any output from the api
    props?: { [key: string]: any };
};

export type ExternalResultMetadata = {
    id?: string;
    title?: string;
    url?: string;
    snippet?: string;
};

export type DataPoints = {
    text: string[];
    images: string[];
    citations: string[];
    external_results_metadata?: ExternalResultMetadata[];
};

export type ResponseContext = {
    data_points: DataPoints;
    followup_questions: string[] | null;
    thoughts: Thoughts[];
    answer?: string;
    confidence_level?: string;
    history_truncated?: boolean;
};

export type ChatAppResponseOrError = {
    message: ResponseMessage;
    delta: ResponseMessage;
    context: ResponseContext;
    session_state: any;
    error?: string;
};

export type ChatAppResponse = {
    message: ResponseMessage;
    delta: ResponseMessage;
    context: ResponseContext;
    session_state: any;
};

export type ChatAppRequestContext = {
    overrides?: ChatAppRequestOverrides;
    example_question_id?: string;
};

export type ChatAppRequest = {
    messages: ResponseMessage[];
    context?: ChatAppRequestContext;
    session_state: any;
};

export type Config = {
    streamingEnabled: boolean;
    showLanguagePicker: boolean;
    showSpeechInput?: boolean;
    showSpeechOutputBrowser?: boolean;
    showSpeechOutputAzure?: boolean;
    showChatHistoryBrowser: boolean;
    showChatHistoryCosmos?: boolean;
    showRealtimeVoice: boolean;
};

export type SimpleAPIResponse = {
    message?: string;
};

export type HistoryListApiResponse = {
    sessions: {
        id: string;
        entra_oid: string;
        email?: string;
        display_name?: string;
        title: string;
        timestamp: number;
    }[];
    continuation_token?: string;
};

export type HistoryApiResponse = {
    id: string;
    entra_oid: string;
    email?: string;
    display_name?: string;
    answers: any;
};

export type AdminSettings = PublicStatus & {
    fun_facts: string[];
    manager_email: string;
};

export type PublicStatus = {
    vacation_mode: boolean;
    vacation_return_date: string | null;
    owner_name: string;
    welcome_message: string;
    owner_email: string;
    calendar_url: string;
    assistant_name: string;
    site_description: string;
    avatar_url: string;
    social_image_url: string;
    linkedin_url: string;
    tone: string;
    example_questions: string[];
};

export type AnalyticsSummary = {
    total_queries: number;
    avg_response_time_ms: number;
    queries_by_day: { date: string; count: number }[];
    confidence_distribution: { high: number; medium: number; low: number; unknown: number };
    recent_queries: {
        query?: string;
        confidence_level: string;
        response_time_ms: number;
        timestamp: number;
        error?: string;
        is_example_question?: boolean;
        message_index?: number;
    }[];
    unique_users: number;
    example_question_rate: number;
    avg_session_depth: number;
    error_count: number;
};
