use assert_json_diff::assert_json_include;
use async_trait::async_trait;
use axum::http::StatusCode;
use axum_test_helper::TestClient;
use capture::api::{CaptureError, CaptureResponse, CaptureResponseCode};
use capture::config::CaptureMode;
use capture::router::router;
use capture::sinks::Event;
use capture::time::TimeSource;
use capture::v0_request::{DataType, ProcessedEvent};
use common_redis::MockRedisClient;
use health::HealthRegistry;
use limiters::redis::{QuotaResource, RedisLimiter, ServiceName, QUOTA_LIMITER_CACHE_KEY};
use limiters::token_dropper::TokenDropper;
use serde::Deserialize;
use serde_json::{json, Value};
use std::fs;
use std::path::Path;
use std::sync::{Arc, Mutex};
use std::time::Duration;
use time::format_description::well_known::{Iso8601, Rfc3339};
use time::OffsetDateTime;

#[derive(Debug, Deserialize)]
struct TestMetadata {
    title: String,
    path: String,
    method: String,
    content_encoding: String,
    content_type: String,
    ip: String,
    now: String,
    #[serde(default)]
    historical_migration: bool,
}

#[derive(Clone)]
pub struct FixedTime {
    pub time: String,
}

impl TimeSource for FixedTime {
    fn current_time(&self) -> String {
        self.time.to_string()
    }
}

#[derive(Clone, Default)]
struct MemorySink {
    events: Arc<Mutex<Vec<ProcessedEvent>>>,
}

impl MemorySink {
    fn len(&self) -> usize {
        self.events.lock().unwrap().len()
    }

    fn events(&self) -> Vec<ProcessedEvent> {
        self.events.lock().unwrap().clone()
    }
}

#[async_trait]
impl Event for MemorySink {
    async fn send(&self, event: ProcessedEvent) -> Result<(), CaptureError> {
        self.events.lock().unwrap().push(event);
        Ok(())
    }

    async fn send_batch(&self, events: Vec<ProcessedEvent>) -> Result<(), CaptureError> {
        self.events.lock().unwrap().extend_from_slice(&events);
        Ok(())
    }
}

fn prepare_capture_payload(
    client: &TestClient,
    metadata: &TestMetadata,
    payload: Vec<u8>,
) -> anyhow::Result<axum_test_helper::RequestBuilder> {
    let mut req = match metadata.method.as_str() {
        "POST" => client.post(&metadata.path).body(payload),
        "GET" => client.get(&metadata.path).body(payload),
        invalid => return Err(anyhow::anyhow!("invalid HTTP method: {}", invalid)),
    };

    // TODO(eli): ADD additional GET params (etc.) and headers as needed (lib_version, _ sent_at etc.)

    if !metadata.content_encoding.is_empty() {
        req = req.header("Content-encoding", &metadata.content_encoding);
    }
    if !metadata.content_type.is_empty() {
        req = req.header("Content-type", &metadata.content_type);
    }
    if !metadata.ip.is_empty() {
        req = req.header("X-Forwarded-For", &metadata.ip);
    }

    Ok(req)
}

// TODO(eli): Compare Vec<ProcessedEvent> from MemorySink to Vec<Value> (from .expected file)
//            and use assert_json_include(...) so we only compare values of interest from .expected
fn load_test_fixture(case_name: &str) -> anyhow::Result<(TestMetadata, Vec<u8>, Vec<Value>)> {
    let fixtures_dir = Path::new("tests/fixtures");

    // Load test case metadata file
    let metadata_path = fixtures_dir.join(format!("{}.json", case_name));
    let metadata = &fs::read_to_string(metadata_path).expect("metadata fixture file not found");
    let metadata: TestMetadata =
        serde_json::from_str(metadata).expect("failed to deserialize metadata fixture file");

    // Load payload fixture file; can be encoded/compressed any ways supported by capture SDKs
    let payload_path = fixtures_dir.join(format!("{}.payload", case_name));
    let payload: Vec<u8> = fs::read(payload_path).expect("payload fixture file not found");

    // Load expected output
    let expected_path = fixtures_dir.join(format!("{}.expected.json", case_name));
    let expected =
        fs::read_to_string(expected_path).expect("expected payload fixture file not found");
    let expected: Vec<Value> =
        serde_json::from_str(&expected).expect("expected payload failed to deserialize");

    Ok((metadata, payload, expected))
}

fn setup_capture_app(now: &str) -> (TestClient, MemorySink) {
    let liveness = HealthRegistry::new("sdk_compat_tests");
    let sink = MemorySink::default();
    let timesource = FixedTime {
        time: now.to_string(),
    };

    let redis = Arc::new(MockRedisClient::new());
    let billing_limiter = RedisLimiter::new(
        Duration::from_secs(60 * 60 * 24 * 7),
        redis.clone(),
        QUOTA_LIMITER_CACHE_KEY.to_string(),
        None,
        QuotaResource::Events,
        ServiceName::Capture,
    )
    .expect("failed to create billing limiter");

    // disable historical rerouting for this test,
    // since we use fixture files with old timestamps
    let enable_historical_rerouting = false;
    let historical_rerouting_threshold_days = 1_i64;
    let historical_tokens_keys = None;
    let is_mirror_deploy = false; // TODO: remove after migration to 100% capture-rs backend
    let base64_detect_percent = 0.0_f32;

    let app = router(
        timesource,
        liveness,
        sink.clone(),
        redis,
        billing_limiter,
        TokenDropper::default(),
        false,
        CaptureMode::Events,
        None,
        25 * 1024 * 1024,
        enable_historical_rerouting,
        historical_rerouting_threshold_days,
        historical_tokens_keys,
        is_mirror_deploy,
        base64_detect_percent,
    );

    (TestClient::new(app), sink)
}

fn assert_events_match(
    actual_events: &[ProcessedEvent],
    expected_events: &[Value],
    historical_migration: bool,
) -> anyhow::Result<()> {
    assert_eq!(
        actual_events.len(),
        expected_events.len(),
        "event count mismatch"
    );

    for (event_number, (message, expected)) in
        actual_events.iter().zip(expected_events.iter()).enumerate()
    {
        // Ensure the data type matches
        if historical_migration {
            assert_eq!(DataType::AnalyticsHistorical, message.metadata.data_type);
        } else {
            assert_eq!(DataType::AnalyticsMain, message.metadata.data_type);
        }

        // Normalizing the expected event to align with known django->rust inconsistencies
        let mut expected = expected.clone();

        if let Some(value) = expected.get_mut("sent_at") {
            // Default ISO format is different between python and rust, both are valid
            // Parse and re-print the value before comparison
            let raw_value = value.as_str().expect("sent_at field is not a string");
            if raw_value.is_empty() {
                *value = Value::Null
            } else {
                let sent_at =
                    OffsetDateTime::parse(value.as_str().expect("empty"), &Iso8601::DEFAULT)
                        .expect("failed to parse expected sent_at");
                *value = Value::String(sent_at.format(&Rfc3339)?)
            }
        }

        if let Some(expected_data) = expected.get_mut("data") {
            // Data is a serialized JSON map. Unmarshall both and compare them,
            // instead of expecting the serialized bytes to be equal
            let mut expected_props: Value =
                serde_json::from_str(expected_data.as_str().expect("not str"))?;
            if let Some(object) = expected_props.as_object_mut() {
                // toplevel fields added by posthog-node that plugin-server will ignore anyway
                object.remove("type");
                object.remove("library");
                object.remove("library_version");
            }

            let found_props: Value = serde_json::from_str(&message.event.data)?;
            let match_config = assert_json_diff::Config::new(assert_json_diff::CompareMode::Strict);
            if let Err(e) =
                assert_json_matches_no_panic(&expected_props, &found_props, match_config)
            {
                anyhow::bail!("data field mismatch at event {}: {}", event_number, e);
            } else {
                *expected_data = json!(&message.event.data)
            }
        }

        if let Some(object) = expected.as_object_mut() {
            // site_url is unused in the pipeline now, let's drop it
            object.remove("site_url");

            // Remove sent_at field if empty: Rust will skip marshalling it
            if let Some(None) = object.get("sent_at").map(|v| v.as_str()) {
                object.remove("sent_at");
            }
        }

        let match_config = assert_json_diff::Config::new(assert_json_diff::CompareMode::Strict);
        if let Err(e) =
            assert_json_matches_no_panic(&json!(expected), &json!(message.event), match_config)
        {
            anyhow::bail!("record mismatch at event {}: {}", event_number, e);
        }
    }

    Ok(())
}

#[tokio::test]
async fn test_posthog_js_identify_event() -> anyhow::Result<()> {
    let (metadata, payload, expected_events) = load_test_fixture("case_1_identify_event")?;

    let (client, sink) = setup_capture_app(&metadata.now);

    let req = prepare_capture_payload(&client, &metadata, payload)?;

    let res = req.send().await;
    assert_eq!(
        res.status(),
        StatusCode::OK,
        "test {} rejected: {}",
        metadata.title,
        res.text().await
    );

    assert_eq!(
        Some(CaptureResponse {
            status: CaptureResponseCode::Ok,
            quota_limited: None,
        }),
        res.json().await
    );

    // Verify the events match expectations
    assert_eq!(
        sink.len(),
        expected_events.len(),
        "event count mismatch for test {}",
        metadata.title
    );

    assert_events_match(
        &sink.events(),
        &expected_events,
        metadata.historical_migration,
    )?;

    Ok(())
}

#[tokio::test]
async fn test_gzipped_pageview_event() -> anyhow::Result<()> {
    let (metadata, payload, expected_events) = load_test_fixture("case_2_gzipped_pageview")?;

    let (client, sink) = setup_capture_app(&metadata.now);

    let req = prepare_capture_payload(&client, &metadata, payload)?;

    let res = req.send().await;
    assert_eq!(
        res.status(),
        StatusCode::OK,
        "test {} rejected: {}",
        metadata.title,
        res.text().await
    );

    assert_eq!(
        Some(CaptureResponse {
            status: CaptureResponseCode::Ok,
            quota_limited: None,
        }),
        res.json().await
    );

    // Verify the events match expectations
    assert_eq!(
        sink.len(),
        expected_events.len(),
        "event count mismatch for test {}",
        metadata.title
    );

    assert_events_match(
        &sink.events(),
        &expected_events,
        metadata.historical_migration,
    )?;

    Ok(())
}

// Example of how to add more test cases:
// #[tokio::test]
// async fn test_posthog_js_pageview_event() -> anyhow::Result<()> {
//     let (metadata, payload, expected_events) = load_test_fixture("case_2_pageview_event")?;
//     // ... same test logic
//     Ok(())
// }
