//! Raw NSPasteboard snapshot / restore (all items, all representations) and its change count.

use objc2::rc::{Retained, autoreleasepool};
use objc2::runtime::ProtocolObject;
use objc2_app_kit::{NSPasteboard, NSPasteboardItem, NSPasteboardWriting};
use objc2_foundation::{NSArray, NSData, NSString};

/// One pasteboard item: every `(type identifier, raw bytes)` representation it offered.
pub type Item = Vec<(String, Vec<u8>)>;

/// Every item on the general pasteboard; `None` if it exceeds `max_bytes` or can't be read.
pub fn save(max_bytes: usize) -> Option<Vec<Item>> {
    autoreleasepool(|_| {
        let pb = NSPasteboard::generalPasteboard();
        let items = pb.pasteboardItems()?;
        let mut out = Vec::with_capacity(items.count());
        let mut total = 0usize;
        for item in items.iter() {
            let mut reps: Item = Vec::new();
            for ty in item.types().iter() {
                if let Some(data) = item.dataForType(&ty) {
                    let bytes = data.to_vec();
                    total += bytes.len();
                    if total > max_bytes {
                        log::warn!("clipboard snapshot over {max_bytes} bytes; falling back");
                        return None;
                    }
                    reps.push((ty.to_string(), bytes));
                }
            }
            if !reps.is_empty() {
                out.push(reps);
            }
        }
        Some(out)
    })
}

pub fn restore(items: &[Item]) -> Result<(), String> {
    autoreleasepool(|_| {
        let pb = NSPasteboard::generalPasteboard();
        let objs: Vec<Retained<ProtocolObject<dyn NSPasteboardWriting>>> = items
            .iter()
            .map(|reps| {
                let item = NSPasteboardItem::new();
                for (ty, bytes) in reps {
                    item.setData_forType(&NSData::with_bytes(bytes), &NSString::from_str(ty));
                }
                ProtocolObject::from_retained(item)
            })
            .collect();
        pb.clearContents();
        if pb.writeObjects(&NSArray::from_retained_slice(&objs)) {
            Ok(())
        } else {
            Err("NSPasteboard rejected the restored items".into())
        }
    })
}

pub fn has_content() -> bool {
    autoreleasepool(|_| {
        NSPasteboard::generalPasteboard()
            .types()
            .is_some_and(|t| t.count() > 0)
    })
}

/// Bumped by every write to the general pasteboard (by any app). Cheap: no data is read.
pub fn change_count() -> i64 {
    autoreleasepool(|_| NSPasteboard::generalPasteboard().changeCount() as i64)
}
