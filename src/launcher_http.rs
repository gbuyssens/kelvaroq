//! The desktop update check uses the Windows HTTPS client already available on the host.

use anyhow::{Result, ensure};
use std::{
    ffi::c_void,
    ptr,
    time::{Duration, Instant},
};
use windows::{
    Win32::Networking::WinHttp::*,
    core::{Error, PCWSTR, w},
};

struct Handle(*mut c_void);

impl Handle {
    fn new(raw: *mut c_void) -> Result<Self> {
        if raw.is_null() {
            Err(Error::from_thread().into())
        } else {
            Ok(Self(raw))
        }
    }
}

impl Drop for Handle {
    fn drop(&mut self) {
        // Each successful WinHTTP open returns one owned handle.
        unsafe {
            let _ = WinHttpCloseHandle(self.0);
        }
    }
}

fn fetch(buffer: &mut [u8]) -> Result<usize> {
    let start = Instant::now();
    let agent =
        windows::core::HSTRING::from(concat!("Velkin-Launcher/", env!("CARGO_PKG_VERSION")));
    // Synchronous calls borrow these handles and buffers only for their duration.
    unsafe {
        let session = Handle::new(WinHttpOpen(
            &agent,
            WINHTTP_ACCESS_TYPE_AUTOMATIC_PROXY,
            PCWSTR::null(),
            PCWSTR::null(),
            0,
        ))?;
        WinHttpSetTimeouts(session.0, 1500, 1500, 1500, 2000)?;
        let connection = Handle::new(WinHttpConnect(session.0, w!("api.github.com"), 443, 0))?;
        let request = Handle::new(WinHttpOpenRequest(
            connection.0,
            w!("GET"),
            w!("/repos/gbuyssens/velkin/releases/latest"),
            PCWSTR::null(),
            PCWSTR::null(),
            ptr::null(),
            WINHTTP_FLAG_SECURE,
        ))?;
        let disabled =
            WINHTTP_DISABLE_COOKIES | WINHTTP_DISABLE_AUTHENTICATION | WINHTTP_DISABLE_REDIRECTS;
        WinHttpSetOption(
            Some(request.0),
            WINHTTP_OPTION_DISABLE_FEATURE,
            Some(&disabled.to_ne_bytes()),
        )?;
        WinHttpSendRequest(request.0, None, None, 0, 0, 0)?;
        WinHttpReceiveResponse(request.0, ptr::null_mut())?;
        let mut status = 0u32;
        let mut size = size_of_val(&status) as u32;
        WinHttpQueryHeaders(
            request.0,
            WINHTTP_QUERY_STATUS_CODE | WINHTTP_QUERY_FLAG_NUMBER,
            PCWSTR::null(),
            Some((&raw mut status).cast()),
            &mut size,
            ptr::null_mut(),
        )?;
        ensure!(status == 200, "release unavailable");
        let mut used = 0;
        loop {
            ensure!(
                start.elapsed() < Duration::from_secs(6) && used < buffer.len(),
                "release response limit"
            );
            let mut count = 0;
            let chunk = &mut buffer[used..];
            WinHttpReadData(
                request.0,
                chunk.as_mut_ptr().cast(),
                chunk.len().min(16384) as u32,
                &mut count,
            )?;
            if count == 0 {
                return Ok(used);
            }
            used += count as usize;
        }
    }
}

/// The WPF launcher calls this on a worker thread. Zero means no usable response.
///
/// # Safety
/// The caller must supply a writable buffer of the given length for this call.
#[unsafe(no_mangle)]
pub unsafe extern "system" fn velkin_latest_release(buffer: *mut u8, length: u32) -> u32 {
    if buffer.is_null() || length == 0 || length > 512 * 1024 {
        return 0;
    }
    let output = unsafe { std::slice::from_raw_parts_mut(buffer, length as usize) };
    fetch(output).unwrap_or(0) as u32
}
