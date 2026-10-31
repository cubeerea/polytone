//! HTTP client for the Polytone Python backend (see CONTRACTS.md "HTTP API").

use std::time::Duration;

use serde::{Deserialize, Serialize};

pub const DEFAULT_BASE_URL: &str = "http://127.0.0.1:8765";
pub const REQUEST_TIMEOUT: Duration = Duration::from_secs(8);
/// `/ingest/typed` may wait ~1.5s for Wispr Flow and then runs the ingest graph (embeddings).
pub const TYPED_TIMEOUT: Duration = Duration::from_secs(30);

pub const MSG_EMPTY: &str = "Type or select your rough intent first";
pub const MSG_NOT_RUNNING: &str = "Polytone backend not running";
pub const MSG_TIMEOUT: &str = "Polytone timed out";

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct GenerateRequest {
    pub intent: String,
    pub persona: Option<String>,
    pub app_context: Option<String>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct GenerateResponse {
    #[serde(default)]
    pub draft_id: String,
    pub text: String,
    #[serde(default)]
    pub persona: String,
    #[serde(default)]
    pub latency_ms: u64,
    /// Opaque to the Rust side; kept for logging / future use.
    #[serde(default)]
    pub constraints: serde_json::Value,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Health {
    pub status: String,
    #[serde(default)]
    pub neo4j: bool,
    #[serde(default)]
    pub llm: String,
    #[serde(default)]
    pub embeddings: String,
}

#[derive(Debug, Clone, Copy, Default, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum InjectMode {
    #[default]
    Paste,
    Type,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct BackendConfig {
    pub active_persona: String,
    pub hotkey: String,
    #[serde(default)]
    pub inject_mode: InjectMode,
    /// Typing capture opt-in (source "typed"). `None` = backend didn't say; never sent back
    /// as null, so a PUT from this struct can't clobber it.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub capture_enabled: Option<bool>,
    /// Bundle ids typing capture listens to.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub capture_apps: Option<Vec<String>>,
}

/// `POST /ingest/typed` body (CONTRACTS.md "Typing capture").
#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct TypedIngestRequest {
    pub text: String,
    pub app: String,
    pub app_name: Option<String>,
    pub after_inject: bool,
}

/// `IngestResult` (only the counts are used on the Rust side).
#[derive(Debug, Clone, Deserialize, PartialEq, Eq)]
pub struct IngestResult {
    #[serde(default)]
    pub accepted: u32,
    #[serde(default)]
    pub rejected: u32,
    #[serde(default)]
    pub reasons: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum BackendError {
    NotRunning,
    Timeout,
    Http { status: u16, body: String },
    Decode(String),
    Other(String),
}

impl BackendError {
    /// Short, user-visible message suitable for a notification.
    pub fn user_message(&self) -> String {
        match self {
            BackendError::NotRunning => MSG_NOT_RUNNING.to_string(),
            BackendError::Timeout => MSG_TIMEOUT.to_string(),
            BackendError::Http { status, body } => {
                // CONTRACTS: 502 {"error":"llm_failed","detail"} · 504 {"error":"llm_timeout"} · 422 empty intent
                let parsed: Option<serde_json::Value> = serde_json::from_str(body).ok();
                let field = |k: &str| {
                    parsed
                        .as_ref()
                        .and_then(|v| v.get(k))
                        .and_then(|v| v.as_str())
                        .map(str::to_string)
                };
                let detail: String = field("detail")
                    .unwrap_or_else(|| body.clone())
                    .chars()
                    .take(140)
                    .collect();
                match (status, field("error").as_deref()) {
                    (_, Some("llm_timeout")) | (504, _) => {
                        format!("{MSG_TIMEOUT} (the model took too long)")
                    }
                    (_, Some("llm_failed")) => format!("Polytone couldn't write a draft: {detail}"),
                    (422, _) => MSG_EMPTY.to_string(),
                    _ => format!("Polytone backend error ({status}): {detail}"),
                }
            }
            BackendError::Decode(e) => format!("Polytone got a bad response: {e}"),
            BackendError::Other(e) => format!("Polytone error: {e}"),
        }
    }
}

impl std::fmt::Display for BackendError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.user_message())
    }
}

impl std::error::Error for BackendError {}

impl From<reqwest::Error> for BackendError {
    fn from(e: reqwest::Error) -> Self {
        if e.is_timeout() {
            BackendError::Timeout
        } else if e.is_connect() {
            BackendError::NotRunning
        } else if e.is_decode() {
            BackendError::Decode(e.to_string())
        } else {
            BackendError::Other(e.to_string())
        }
    }
}

#[derive(Debug, Clone)]
pub struct BackendClient {
    base_url: String,
    http: reqwest::Client,
}

impl Default for BackendClient {
    fn default() -> Self {
        Self::new(
            std::env::var("POLYTONE_API_URL").unwrap_or_else(|_| DEFAULT_BASE_URL.to_string()),
        )
    }
}

impl BackendClient {
    pub fn new(base_url: impl Into<String>) -> Self {
        let http = reqwest::Client::builder()
            .timeout(REQUEST_TIMEOUT)
            .connect_timeout(Duration::from_secs(2))
            .no_proxy()
            .build()
            .expect("failed to build reqwest client");
        Self {
            base_url: base_url.into().trim_end_matches('/').to_string(),
            http,
        }
    }

    fn url(&self, path: &str) -> String {
        format!("{}{}", self.base_url, path)
    }

    async fn check(resp: reqwest::Response) -> Result<reqwest::Response, BackendError> {
        let status = resp.status();
        if status.is_success() {
            Ok(resp)
        } else {
            let body = resp.text().await.unwrap_or_default();
            Err(BackendError::Http {
                status: status.as_u16(),
                body,
            })
        }
    }

    pub async fn health(&self) -> Result<Health, BackendError> {
        let resp = self
            .http
            .get(self.url("/health"))
            .timeout(Duration::from_secs(2))
            .send()
            .await?;
        Ok(Self::check(resp).await?.json().await?)
    }

    pub async fn generate(&self, req: &GenerateRequest) -> Result<GenerateResponse, BackendError> {
        let resp = self
            .http
            .post(self.url("/generate"))
            .json(req)
            .send()
            .await?;
        Ok(Self::check(resp).await?.json().await?)
    }

    pub async fn get_config(&self) -> Result<BackendConfig, BackendError> {
        let resp = self.http.get(self.url("/config")).send().await?;
        Ok(Self::check(resp).await?.json().await?)
    }

    pub async fn put_config(&self, cfg: &BackendConfig) -> Result<BackendConfig, BackendError> {
        let resp = self.http.put(self.url("/config")).json(cfg).send().await?;
        Ok(Self::check(resp).await?.json().await?)
    }

    pub async fn ingest_typed(
        &self,
        req: &TypedIngestRequest,
    ) -> Result<IngestResult, BackendError> {
        let resp = self
            .http
            .post(self.url("/ingest/typed"))
            .timeout(TYPED_TIMEOUT)
            .json(req)
            .send()
            .await?;
        Ok(Self::check(resp).await?.json().await?)
    }

    /// GET /config, apply `f`, PUT it back.
    pub async fn update_config(
        &self,
        f: impl FnOnce(&mut BackendConfig),
    ) -> Result<BackendConfig, BackendError> {
        let mut cfg = self.get_config().await?;
        f(&mut cfg);
        self.put_config(&cfg).await
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn config_roundtrip_and_inject_mode_default() {
        let cfg: BackendConfig =
            serde_json::from_str(r#"{"active_persona":"Boss","hotkey":"CmdOrCtrl+Shift+P"}"#)
                .unwrap();
        assert_eq!(cfg.inject_mode, InjectMode::Paste);
        let cfg: BackendConfig =
            serde_json::from_str(r#"{"active_persona":"Boss","hotkey":"X","inject_mode":"type"}"#)
                .unwrap();
        assert_eq!(cfg.inject_mode, InjectMode::Type);
        let s = serde_json::to_string(&cfg).unwrap();
        assert!(s.contains(r#""inject_mode":"type""#));
    }

    #[test]
    fn config_capture_fields_are_optional_and_not_clobbered() {
        let cfg: BackendConfig =
            serde_json::from_str(r#"{"active_persona":"Boss","hotkey":"X"}"#).unwrap();
        assert_eq!(cfg.capture_enabled, None);
        let v = serde_json::to_value(&cfg).unwrap();
        assert!(v.get("capture_enabled").is_none() && v.get("capture_apps").is_none());
        let cfg: BackendConfig = serde_json::from_str(
            r#"{"active_persona":"Boss","hotkey":"X","capture_enabled":true,"capture_apps":["com.apple.mail"]}"#,
        )
        .unwrap();
        assert_eq!(cfg.capture_enabled, Some(true));
        assert_eq!(
            cfg.capture_apps.as_deref(),
            Some(&["com.apple.mail".to_string()][..])
        );
    }

    #[test]
    fn typed_request_shape() {
        let r = TypedIngestRequest {
            text: "hi there you".into(),
            app: "com.apple.mail".into(),
            app_name: None,
            after_inject: true,
        };
        assert_eq!(
            serde_json::to_value(&r).unwrap(),
            serde_json::json!({"text":"hi there you","app":"com.apple.mail","app_name":null,"after_inject":true})
        );
    }

    #[test]
    fn generate_request_serializes_nulls() {
        let r = GenerateRequest {
            intent: "hi".into(),
            persona: None,
            app_context: None,
        };
        let v = serde_json::to_value(&r).unwrap();
        assert_eq!(
            v,
            serde_json::json!({"intent":"hi","persona":null,"app_context":null})
        );
    }

    #[test]
    fn generate_response_parses() {
        let r: GenerateResponse = serde_json::from_str(
            r#"{"draft_id":"d1","text":"Hello","persona":"Boss","latency_ms":42,"constraints":{"avg_sentence_len":1.0}}"#,
        )
        .unwrap();
        assert_eq!(r.text, "Hello");
        assert_eq!(r.latency_ms, 42);
    }

    #[test]
    fn user_messages() {
        assert_eq!(BackendError::NotRunning.user_message(), MSG_NOT_RUNNING);
        assert_eq!(BackendError::Timeout.user_message(), MSG_TIMEOUT);
        let http = |status: u16, body: &str| {
            BackendError::Http {
                status,
                body: body.into(),
            }
            .user_message()
        };
        assert_eq!(
            http(
                502,
                r#"{"error":"llm_failed","detail":"no LLM configured"}"#
            ),
            "Polytone couldn't write a draft: no LLM configured"
        );
        assert!(http(504, r#"{"error":"llm_timeout","detail":"x"}"#).starts_with(MSG_TIMEOUT));
        assert_eq!(http(422, r#"{"detail":"intent is empty"}"#), MSG_EMPTY);
        assert_eq!(
            http(500, "Internal Server Error"),
            "Polytone backend error (500): Internal Server Error"
        );
    }

    #[test]
    fn unreachable_backend_maps_to_not_running() {
        // Port 9 (discard) on localhost is essentially never listening.
        let c = BackendClient::new("http://127.0.0.1:9");
        let rt = tokio_rt();
        let err = rt.block_on(c.health()).unwrap_err();
        assert_eq!(err, BackendError::NotRunning);
    }

    fn tokio_rt() -> tokio::runtime::Runtime {
        tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap()
    }
}
