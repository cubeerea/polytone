//! HTTP client for the Polytone Python backend (see CONTRACTS.md "HTTP API").

use std::path::PathBuf;
use std::sync::{Arc, RwLock};
use std::time::Duration;

use serde::{Deserialize, Serialize};

pub const DEFAULT_BASE_URL: &str = "http://127.0.0.1:8765";
pub const REQUEST_TIMEOUT: Duration = Duration::from_secs(8);
/// `/ingest/typed` may wait ~1.5s for Wispr Flow and then runs the ingest graph (embeddings).
pub const TYPED_TIMEOUT: Duration = Duration::from_secs(30);

pub const MSG_EMPTY: &str = "Type or select your rough intent first";
pub const MSG_NOT_RUNNING: &str = "Polytone backend not running";
pub const MSG_TIMEOUT: &str = "Polytone timed out";
pub const MSG_UNTRUSTED: &str =
    "Another program is using Polytone's port, so nothing was sent. Quit it and restart Polytone";

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

/// `POST /session` response (CONTRACTS.md "Security"): a UI session token.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct SessionInfo {
    pub token: String,
    #[serde(default)]
    pub idle_timeout_s: u64,
    #[serde(default)]
    pub expires_at: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum BackendError {
    NotRunning,
    Timeout,
    Http {
        status: u16,
        body: String,
    },
    Decode(String),
    /// Whatever answers on the port couldn't prove it knows the app token: not our backend.
    Untrusted,
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
            BackendError::Untrusted => MSG_UNTRUSTED.to_string(),
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

/// The app token (CONTRACTS.md "Security"): only Rust holds it; it never reaches the webview.
#[derive(Debug, Default)]
struct Auth {
    token: Option<String>,
    /// `<data_dir>/.api_token`: re-read once on a 401 (the backend may have been restarted by
    /// someone else and generated a new token).
    token_file: Option<PathBuf>,
    /// The process on the port proved it holds `token` (`/health?challenge=`).
    verified: bool,
}

#[derive(Debug, Clone)]
pub struct BackendClient {
    base_url: String,
    http: reqwest::Client,
    /// Shared by every clone, so a token set at startup reaches the hotkey / capture paths.
    auth: Arc<RwLock<Auth>>,
    /// Refuse to send anything until the backend proves it knows the app token. On in the app;
    /// off for `new()` so request-shape tests needn't speak the challenge.
    require_proof: bool,
}

impl Default for BackendClient {
    fn default() -> Self {
        let mut c = Self::new(
            std::env::var("POLYTONE_API_URL").unwrap_or_else(|_| DEFAULT_BASE_URL.to_string()),
        );
        c.require_proof = true;
        c
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
            auth: Arc::new(RwLock::new(Auth::default())),
            require_proof: false,
        }
    }

    /// Prove the process on the port is our backend before it sees any data: it must answer
    /// `GET /health?challenge=<c>` with `proof` = hex HMAC-SHA256(app token, "polytone-health-v1:" + c).
    /// Another local program that grabbed 127.0.0.1:8765 can't read the 0600 token file.
    pub async fn verify(&self) -> Result<(), BackendError> {
        let Some(token) = self.auth.read().unwrap().token.clone() else {
            return Err(BackendError::Untrusted);
        };
        let challenge = crate::security::generate_token();
        let resp = self
            .http
            // base64url: safe in a query string as is
            .get(self.url(&format!("/health?challenge={challenge}")))
            .timeout(Duration::from_secs(2))
            .send()
            .await?;
        let body: serde_json::Value = Self::check(resp).await?.json().await?;
        let proof = body.get("proof").and_then(|p| p.as_str()).unwrap_or("");
        if crate::security::health_proof_matches(&token, &challenge, proof) {
            let mut auth = self.auth.write().unwrap();
            if auth.token.as_deref() == Some(token.as_str()) {
                auth.verified = true;
            }
            Ok(())
        } else {
            log::warn!(
                "the process on the backend port failed the token proof; not sending it data"
            );
            Err(BackendError::Untrusted)
        }
    }

    async fn ensure_verified(&self) -> Result<(), BackendError> {
        if !self.require_proof || self.auth.read().unwrap().verified {
            return Ok(());
        }
        self.verify().await
    }

    fn url(&self, path: &str) -> String {
        format!("{}{}", self.base_url, path)
    }

    /// Use `token` as the app token (the one we pass to a backend we spawn).
    pub fn set_token(&self, token: Option<String>) {
        let mut auth = self.auth.write().unwrap();
        auth.token = token;
        auth.verified = false;
    }

    pub fn has_token(&self) -> bool {
        self.auth.read().unwrap().token.is_some()
    }

    /// Where the backend writes its token; re-read on a 401.
    pub fn set_token_file(&self, path: Option<PathBuf>) {
        self.auth.write().unwrap().token_file = path;
    }

    /// Load the token from the token file (attaching to an already-running backend).
    /// Returns whether a token was found.
    pub fn load_token_file(&self) -> bool {
        let mut auth = self.auth.write().unwrap();
        match auth
            .token_file
            .as_deref()
            .and_then(crate::security::read_token_file)
        {
            Some(t) => {
                auth.token = Some(t);
                auth.verified = false;
                true
            }
            None => false,
        }
    }

    /// After a 401: re-read the token file; true if it holds a different token (worth a retry).
    fn refresh_token_after_401(&self) -> bool {
        let mut auth = self.auth.write().unwrap();
        let Some(fresh) = auth
            .token_file
            .as_deref()
            .and_then(crate::security::read_token_file)
        else {
            return false;
        };
        if auth.token.as_deref() == Some(fresh.as_str()) {
            return false;
        }
        log::info!("backend rejected the app token; using the one in the token file");
        auth.token = Some(fresh);
        auth.verified = false;
        true
    }

    /// Send a request with `Authorization: Bearer <app token>`. On 401, re-read the token file
    /// once and retry if it changed. `build` is called once per attempt.
    async fn send(
        &self,
        build: impl Fn(&reqwest::Client) -> reqwest::RequestBuilder,
    ) -> Result<reqwest::Response, BackendError> {
        let attempt = || {
            let token = self.auth.read().unwrap().token.clone();
            let mut req = build(&self.http);
            if let Some(t) = token {
                req = req.bearer_auth(t);
            }
            req.send()
        };
        self.ensure_verified().await?;
        let resp = attempt().await?;
        if resp.status() == reqwest::StatusCode::UNAUTHORIZED && self.refresh_token_after_401() {
            self.ensure_verified().await?;
            return Self::check(attempt().await?).await;
        }
        Self::check(resp).await
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
        // Unauthenticated by contract; no token sent.
        let resp = self
            .http
            .get(self.url("/health"))
            .timeout(Duration::from_secs(2))
            .send()
            .await?;
        Ok(Self::check(resp).await?.json().await?)
    }

    pub async fn generate(&self, req: &GenerateRequest) -> Result<GenerateResponse, BackendError> {
        let url = self.url("/generate");
        Ok(self.send(|h| h.post(&url).json(req)).await?.json().await?)
    }

    pub async fn get_config(&self) -> Result<BackendConfig, BackendError> {
        let url = self.url("/config");
        Ok(self.send(|h| h.get(&url)).await?.json().await?)
    }

    pub async fn put_config(&self, cfg: &BackendConfig) -> Result<BackendConfig, BackendError> {
        let url = self.url("/config");
        Ok(self.send(|h| h.put(&url).json(cfg)).await?.json().await?)
    }

    pub async fn ingest_typed(
        &self,
        req: &TypedIngestRequest,
    ) -> Result<IngestResult, BackendError> {
        let url = self.url("/ingest/typed");
        Ok(self
            .send(|h| h.post(&url).timeout(TYPED_TIMEOUT).json(req))
            .await?
            .json()
            .await?)
    }

    /// `POST /session` (app token) -> a UI session token.
    pub async fn create_session(&self) -> Result<SessionInfo, BackendError> {
        let url = self.url("/session");
        Ok(self.send(|h| h.post(&url)).await?.json().await?)
    }

    /// `DELETE /session` (app token): revoke every UI session.
    pub async fn revoke_sessions(&self) -> Result<(), BackendError> {
        let url = self.url("/session");
        self.send(|h| h.delete(&url)).await.map(|_| ())
    }

    /// `POST /audit/event` (app token): a Rust-side audit event (never user text).
    pub async fn audit_event(
        &self,
        event: &str,
        detail: &serde_json::Value,
    ) -> Result<(), BackendError> {
        let url = self.url("/audit/event");
        let body = serde_json::json!({ "event": event, "detail": detail });
        self.send(|h| h.post(&url).json(&body).timeout(Duration::from_secs(3)))
            .await
            .map(|_| ())
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

    // ----- auth against a tiny mock HTTP server -----

    use std::sync::Mutex;
    use tokio::io::{AsyncReadExt, AsyncWriteExt};

    /// Recorded requests: ("METHOD /path", Authorization header).
    type Seen = Arc<Mutex<Vec<(String, Option<String>)>>>;

    /// Serves `respond("METHOD /path", auth header) -> (status, body)` on an ephemeral port.
    async fn mock_server(
        respond: impl Fn(&str, Option<&str>) -> (u16, String) + Send + Sync + 'static,
    ) -> (String, Seen) {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        let seen: Seen = Arc::default();
        let seen2 = seen.clone();
        let respond = Arc::new(respond);
        tokio::spawn(async move {
            loop {
                let Ok((mut sock, _)) = listener.accept().await else {
                    return;
                };
                let seen = seen2.clone();
                let respond = respond.clone();
                tokio::spawn(async move {
                    let mut buf = Vec::new();
                    let mut chunk = [0u8; 4096];
                    // Headers only; the bodies these tests send are ignored.
                    while !buf.windows(4).any(|w| w == b"\r\n\r\n") {
                        let n = sock.read(&mut chunk).await.unwrap_or(0);
                        if n == 0 {
                            return;
                        }
                        buf.extend_from_slice(&chunk[..n]);
                    }
                    let text = String::from_utf8_lossy(&buf).to_string();
                    let mut lines = text.split("\r\n");
                    let first = lines.next().unwrap_or_default();
                    let line = first.split(' ').take(2).collect::<Vec<_>>().join(" ");
                    let auth = lines.find_map(|l| {
                        l.split_once(':')
                            .filter(|(k, _)| k.eq_ignore_ascii_case("authorization"))
                            .map(|(_, v)| v.trim().to_string())
                    });
                    seen.lock().unwrap().push((line.clone(), auth.clone()));
                    let (status, body) = respond(&line, auth.as_deref());
                    let resp = format!(
                        "HTTP/1.1 {status} X\r\ncontent-type: application/json\r\ncontent-length: {}\r\nconnection: close\r\n\r\n{body}",
                        body.len()
                    );
                    let _ = sock.write_all(resp.as_bytes()).await;
                    let _ = sock.shutdown().await;
                });
            }
        });
        (format!("http://{addr}"), seen)
    }

    const CFG: &str = r#"{"active_persona":"Boss","hotkey":"X"}"#;

    fn only_token(tok: &'static str) -> impl Fn(&str, Option<&str>) -> (u16, String) {
        move |_line, auth| {
            if auth == Some(format!("Bearer {tok}").as_str()) {
                (200, CFG.to_string())
            } else {
                (401, r#"{"detail":"locked"}"#.to_string())
            }
        }
    }

    fn temp_dir(name: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!("polytone-test-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(&d).unwrap();
        d
    }

    #[test]
    fn sends_bearer_token() {
        tokio_rt().block_on(async {
            let (url, seen) = mock_server(only_token("tok-a")).await;
            let c = BackendClient::new(url);
            c.set_token(Some("tok-a".into()));
            assert_eq!(c.get_config().await.unwrap().active_persona, "Boss");
            let seen = seen.lock().unwrap();
            assert_eq!(seen.len(), 1);
            assert_eq!(seen[0].0, "GET /config");
            assert_eq!(seen[0].1.as_deref(), Some("Bearer tok-a"));
        });
    }

    #[test]
    fn health_sends_no_token() {
        tokio_rt().block_on(async {
            let (url, seen) = mock_server(|_, _| (200, r#"{"status":"ok"}"#.into())).await;
            let c = BackendClient::new(url);
            c.set_token(Some("secret".into()));
            c.health().await.unwrap();
            assert_eq!(seen.lock().unwrap()[0].1, None);
        });
    }

    #[test]
    fn retries_once_with_token_file_after_401() {
        let dir = temp_dir("retry");
        let file = dir.join(".api_token");
        std::fs::write(&file, "tok-new\n").unwrap();
        tokio_rt().block_on(async {
            let (url, seen) = mock_server(only_token("tok-new")).await;
            let c = BackendClient::new(url);
            c.set_token(Some("tok-old".into()));
            c.set_token_file(Some(file.clone()));
            assert!(c.get_config().await.is_ok());
            let got = seen.lock().unwrap().clone();
            assert_eq!(got.len(), 2);
            assert_eq!(got[0].1.as_deref(), Some("Bearer tok-old"));
            assert_eq!(got[1].1.as_deref(), Some("Bearer tok-new"));
            // The fresh token sticks for later requests, on every clone.
            let c2 = c.clone();
            assert!(c2.get_config().await.is_ok());
            assert_eq!(seen.lock().unwrap().len(), 3);
        });
        let _ = std::fs::remove_dir_all(dir);
    }

    #[test]
    fn no_retry_when_token_file_unchanged_or_missing() {
        let dir = temp_dir("noretry");
        let file = dir.join(".api_token");
        std::fs::write(&file, "tok-same").unwrap();
        tokio_rt().block_on(async {
            let (url, seen) = mock_server(only_token("other")).await;
            let c = BackendClient::new(url);
            c.set_token(Some("tok-same".into()));
            c.set_token_file(Some(file.clone()));
            let err = c.get_config().await.unwrap_err();
            assert!(matches!(err, BackendError::Http { status: 401, .. }));
            assert_eq!(seen.lock().unwrap().len(), 1);

            // Missing file: one request, no retry.
            c.set_token_file(Some(dir.join("missing")));
            assert!(c.get_config().await.is_err());
            assert_eq!(seen.lock().unwrap().len(), 2);

            // A changed file buys exactly one retry, even if that 401s too.
            std::fs::write(&file, "tok-third").unwrap();
            c.set_token_file(Some(file.clone()));
            assert!(c.get_config().await.is_err());
            assert_eq!(seen.lock().unwrap().len(), 4);
        });
        let _ = std::fs::remove_dir_all(dir);
    }

    #[test]
    fn attach_loads_token_file() {
        let dir = temp_dir("attach");
        let file = dir.join(".api_token");
        let c = BackendClient::new("http://127.0.0.1:9");
        c.set_token_file(Some(file.clone()));
        assert!(!c.load_token_file());
        assert!(!c.has_token());
        std::fs::write(&file, "  abc  \n").unwrap();
        assert!(c.load_token_file());
        assert_eq!(c.auth.read().unwrap().token.as_deref(), Some("abc"));
        let _ = std::fs::remove_dir_all(dir);
    }

    #[test]
    fn session_and_audit_endpoints() {
        tokio_rt().block_on(async {
            let (url, seen) = mock_server(|line, auth| {
                if auth != Some("Bearer app") {
                    return (401, r#"{"detail":"locked"}"#.into());
                }
                match line {
                    "POST /session" => (
                        200,
                        r#"{"token":"s1","idle_timeout_s":900,"expires_at":"2026-01-01T00:00:00Z"}"#
                            .into(),
                    ),
                    "DELETE /session" | "POST /audit/event" => (200, "{}".into()),
                    _ => (404, "{}".into()),
                }
            })
            .await;
            let c = BackendClient::new(url);
            c.set_token(Some("app".into()));
            let s = c.create_session().await.unwrap();
            assert_eq!(s.token, "s1");
            assert_eq!(s.idle_timeout_s, 900);
            c.revoke_sessions().await.unwrap();
            c.audit_event("session.lock", &serde_json::json!({"reason":"tray"}))
                .await
                .unwrap();
            let lines: Vec<String> = seen.lock().unwrap().iter().map(|s| s.0.clone()).collect();
            assert_eq!(
                lines,
                ["POST /session", "DELETE /session", "POST /audit/event"]
            );
        });
    }

    fn proof_for(token: &str, line: &str) -> String {
        use hmac::{Hmac, Mac};
        let challenge = line.split("challenge=").nth(1).unwrap_or("");
        let mut mac = Hmac::<sha2::Sha256>::new_from_slice(token.as_bytes()).unwrap();
        mac.update(b"polytone-health-v1:");
        mac.update(challenge.as_bytes());
        hex::encode(mac.finalize().into_bytes())
    }

    #[test]
    fn app_client_sends_nothing_to_an_impostor() {
        tokio_rt().block_on(async {
            // Answers /health like a backend but doesn't know the token.
            let (url, seen) = mock_server(|line, _| {
                if line.starts_with("GET /health") {
                    (200, r#"{"status":"ok","proof":"00"}"#.into())
                } else {
                    (200, CFG.into())
                }
            })
            .await;
            let mut c = BackendClient::new(url);
            c.require_proof = true;
            c.set_token(Some("app".into()));
            assert!(matches!(c.get_config().await, Err(BackendError::Untrusted)));
            let lines: Vec<String> = seen.lock().unwrap().iter().map(|s| s.0.clone()).collect();
            assert_eq!(lines.len(), 1);
            assert!(lines[0].starts_with("GET /health?challenge="));
            assert_eq!(
                seen.lock().unwrap()[0].1,
                None,
                "token must not go to the challenge"
            );
        });
    }

    #[test]
    fn app_client_verifies_once_then_sends() {
        tokio_rt().block_on(async {
            let (url, seen) = mock_server(|line, auth| {
                if line.starts_with("GET /health") {
                    (
                        200,
                        format!(r#"{{"status":"ok","proof":"{}"}}"#, proof_for("app", line)),
                    )
                } else if auth == Some("Bearer app") {
                    (200, CFG.into())
                } else {
                    (401, r#"{"detail":"locked"}"#.into())
                }
            })
            .await;
            let mut c = BackendClient::new(url);
            c.require_proof = true;
            c.set_token(Some("app".into()));
            c.get_config().await.unwrap();
            c.get_config().await.unwrap();
            let n_health = seen
                .lock()
                .unwrap()
                .iter()
                .filter(|s| s.0.starts_with("GET /health"))
                .count();
            assert_eq!(n_health, 1);
        });
    }

    #[test]
    fn proof_check_rejects_garbage() {
        assert!(!crate::security::health_proof_matches("t", "c", "zz"));
        assert!(!crate::security::health_proof_matches("t", "c", ""));
        let ok = proof_for("t", "x?challenge=c");
        assert!(crate::security::health_proof_matches("t", "c", &ok));
        assert!(!crate::security::health_proof_matches("u", "c", &ok));
    }

    fn tokio_rt() -> tokio::runtime::Runtime {
        tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap()
    }
}
