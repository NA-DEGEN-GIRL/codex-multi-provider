use serde::Deserialize;
use serde_json::{Value, json};
use tokio::io::{AsyncBufRead, AsyncBufReadExt};

pub const VERSION: u64 = 27;
pub const REQUEST_LIMIT: usize = 256 * 1024;
pub const RESPONSE_LIMIT: usize = 8 * 1024 * 1024;

#[derive(Deserialize)]
pub struct Request {
    pub id: String,
    pub command: String,
    #[serde(default)]
    pub version: u64,
    pub args: Value,
}

pub fn ok(id: &str, result: Value) -> Value {
    json!({"id":id,"ok":true,"result":result})
}
pub fn error(id: &str, code: &str, message: &str) -> Value {
    json!({"id":id,"ok":false,"error":{"code":code,"message":message}})
}

pub async fn frame<R: AsyncBufRead + Unpin>(
    reader: &mut R,
    limit: usize,
) -> std::io::Result<Option<Vec<u8>>> {
    let mut out = Vec::new();
    loop {
        let input = reader.fill_buf().await?;
        if input.is_empty() {
            return if out.is_empty() {
                Ok(None)
            } else {
                Err(std::io::ErrorKind::UnexpectedEof.into())
            };
        }
        let end = input.iter().position(|b| *b == b'\n');
        let count = end.unwrap_or(input.len());
        if out.len() + count > limit {
            return Err(std::io::ErrorKind::InvalidData.into());
        }
        out.extend_from_slice(&input[..count]);
        reader.consume(count + usize::from(end.is_some()));
        if end.is_some() {
            return Ok(Some(out));
        }
    }
}

pub fn uuid(value: &Value, key: &str) -> Result<String, String> {
    uuid::Uuid::parse_str(value[key].as_str().ok_or("식별자가 없습니다.")?)
        .map(|id| id.to_string())
        .map_err(|_| "식별자가 올바르지 않습니다.".into())
}
