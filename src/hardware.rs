//! Capabilities of the Windows host used by the launcher.
use crate::adapters;
use serde::Serialize;
use windows::{
    Win32::{
        Graphics::{Direct3D12::*, Dxgi::*},
        System::SystemInformation::*,
    },
    core::Interface,
};

#[derive(Default, Serialize)]
struct Gpu {
    name: String,
    vendor: u32,
    vram: u64,
    driver: Option<String>,
    fl12: bool,
    uma: Option<bool>,
    sm66: Option<bool>,
    mesh: Option<bool>,
    bindless: Option<bool>,
    root11: Option<bool>,
    barriers: Option<bool>,
    upload_heap: Option<bool>,
    wave_min: u32,
    wave_max: u32,
}

impl Gpu {
    /// Approach after XCL 8ba374c: query shader support and wave widths on the
    /// selected device. Different generations from one vendor have different limits.
    fn read(adapter: IDXGIAdapter1, desc: DXGI_ADAPTER_DESC1) -> Self {
        let mut gpu = Self {
            name: adapters::name(&desc),
            vendor: desc.VendorId,
            vram: desc.DedicatedVideoMemory as u64,
            driver: unsafe { adapter.CheckInterfaceSupport(&IDXGIDevice::IID) }
                .ok()
                .map(|v| {
                    format!(
                        "{}.{}.{}.{}",
                        (v >> 48) as u16,
                        (v >> 32) as u16,
                        (v >> 16) as u16,
                        v as u16
                    )
                }),
            ..Self::default()
        };
        let Some(device) = adapters::device(&adapter) else {
            return gpu;
        };
        gpu.fl12 = true;
        // Each feature ID takes a specific SDK structure. Failure of a newer
        // optional query must leave the shader and memory results available.
        macro_rules! query {
            ($id:expr, $initial:expr) => {{
                let mut value = $initial;
                unsafe {
                    device.CheckFeatureSupport(
                        $id,
                        (&raw mut value).cast(),
                        size_of_val(&value) as u32,
                    )
                }
                .ok()
                .map(|()| value)
            }};
        }
        gpu.sm66 = query!(
            D3D12_FEATURE_SHADER_MODEL,
            D3D12_FEATURE_DATA_SHADER_MODEL {
                HighestShaderModel: D3D_SHADER_MODEL_6_6
            }
        )
        .map(|v| v.HighestShaderModel.0 >= D3D_SHADER_MODEL_6_6.0);
        gpu.mesh = query!(
            D3D12_FEATURE_D3D12_OPTIONS7,
            D3D12_FEATURE_DATA_D3D12_OPTIONS7::default()
        )
        .map(|v| v.MeshShaderTier.0 > D3D12_MESH_SHADER_TIER_NOT_SUPPORTED.0);
        gpu.bindless = query!(
            D3D12_FEATURE_D3D12_OPTIONS,
            D3D12_FEATURE_DATA_D3D12_OPTIONS::default()
        )
        .map(|v| v.ResourceBindingTier.0 >= D3D12_RESOURCE_BINDING_TIER_3.0);
        gpu.root11 = query!(
            D3D12_FEATURE_ROOT_SIGNATURE,
            D3D12_FEATURE_DATA_ROOT_SIGNATURE {
                HighestVersion: D3D_ROOT_SIGNATURE_VERSION_1_1
            }
        )
        .map(|v| v.HighestVersion.0 >= D3D_ROOT_SIGNATURE_VERSION_1_1.0);
        gpu.uma = query!(
            D3D12_FEATURE_ARCHITECTURE1,
            D3D12_FEATURE_DATA_ARCHITECTURE1::default()
        )
        .map(|v| v.UMA.as_bool());
        gpu.barriers = query!(
            D3D12_FEATURE_D3D12_OPTIONS12,
            D3D12_FEATURE_DATA_D3D12_OPTIONS12::default()
        )
        .map(|v| v.EnhancedBarriersSupported.as_bool());
        gpu.upload_heap = query!(
            D3D12_FEATURE_D3D12_OPTIONS16,
            D3D12_FEATURE_DATA_D3D12_OPTIONS16::default()
        )
        .map(|v| v.GPUUploadHeapSupported.as_bool());
        if let Some(waves) = query!(
            D3D12_FEATURE_D3D12_OPTIONS1,
            D3D12_FEATURE_DATA_D3D12_OPTIONS1::default()
        ) && waves.WaveOps.as_bool()
        {
            gpu.wave_min = waves.WaveLaneCountMin;
            gpu.wave_max = waves.WaveLaneCountMax;
        }
        gpu
    }
}

/// Each variable-length core record carries one mask per processor group.
/// Counting set bits includes SMT siblings and cores beyond the first 64 threads.
fn count_cores(mut bytes: &[u8]) -> Option<(u32, u32)> {
    use std::mem::offset_of;
    let base = offset_of!(SYSTEM_LOGICAL_PROCESSOR_INFORMATION_EX, Anonymous);
    let count_at = base + offset_of!(PROCESSOR_RELATIONSHIP, GroupCount);
    let masks_at = base + offset_of!(PROCESSOR_RELATIONSHIP, GroupMask);
    let (mut physical, mut logical) = (0, 0);
    while !bytes.is_empty() {
        let size = u32::from_ne_bytes(bytes.get(4..8)?.try_into().ok()?) as usize;
        let (record, rest) = bytes.split_at_checked(size)?;
        let groups = u16::from_ne_bytes(record.get(count_at..count_at + 2)?.try_into().ok()?);
        if groups == 0 {
            return None;
        }
        let end = masks_at + usize::from(groups) * size_of::<GROUP_AFFINITY>();
        for group in record
            .get(masks_at..end)?
            .as_chunks::<{ size_of::<GROUP_AFFINITY>() }>()
            .0
        {
            logical +=
                usize::from_ne_bytes(group[..size_of::<usize>()].try_into().ok()?).count_ones();
        }
        physical += 1;
        bytes = rest;
    }
    (physical > 0 && logical >= physical).then_some((physical, logical))
}

fn topology() -> Option<(u32, u32)> {
    let mut len = 0;
    let _ = unsafe { GetLogicalProcessorInformationEx(RelationProcessorCore, None, &mut len) };
    for _ in 0..2 {
        if len == 0 {
            return None;
        }
        let mut words = vec![0usize; (len as usize).div_ceil(size_of::<usize>())];
        let capacity = words.len() * size_of::<usize>();
        if unsafe {
            GetLogicalProcessorInformationEx(
                RelationProcessorCore,
                Some(words.as_mut_ptr().cast()),
                &mut len,
            )
        }
        .is_ok()
        {
            let bytes =
                unsafe { std::slice::from_raw_parts(words.as_ptr().cast::<u8>(), capacity) };
            return count_cores(bytes.get(..len as usize)?);
        }
    }
    None
}

#[derive(Serialize)]
struct Machine {
    gpus: Vec<Gpu>,
    physical: u32,
    logical: u32,
    ram: Option<u64>,
}

impl Machine {
    fn read() -> Self {
        let gpus = unsafe { CreateDXGIFactory1::<IDXGIFactory6>() }
            .map(|factory| {
                adapters::hardware(&factory)
                    .map_while(Result::ok)
                    .map(|(adapter, desc)| Gpu::read(adapter, desc))
                    .collect()
            })
            .unwrap_or_default();
        let (physical, logical) = topology().unwrap_or_default();
        let mut memory = MEMORYSTATUSEX {
            dwLength: size_of::<MEMORYSTATUSEX>() as u32,
            ..Default::default()
        };
        let ram = unsafe { GlobalMemoryStatusEx(&mut memory) }
            .ok()
            .map(|_| memory.ullTotalPhys);
        Self {
            gpus,
            physical,
            logical,
            ram,
        }
    }
}

/// Writes a UTF-8 snapshot into the desktop companion's buffer.
/// A zero return lets the launcher continue with an unavailable profile.
///
/// # Safety
/// The caller supplies a writable buffer of at least `capacity` bytes.
#[unsafe(no_mangle)]
pub unsafe extern "system" fn velkin_hardware(output: *mut u8, capacity: usize) -> usize {
    if output.is_null() {
        return 0;
    }
    let Ok(json) = serde_json::to_vec(&Machine::read()) else {
        return 0;
    };
    if json.len() > capacity {
        return 0;
    }
    unsafe { std::ptr::copy_nonoverlapping(json.as_ptr(), output, json.len()) };
    json.len()
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn variable_length_core_records_include_every_group() {
        use std::mem::offset_of;
        let base = offset_of!(SYSTEM_LOGICAL_PROCESSOR_INFORMATION_EX, Anonymous);
        let masks = base + offset_of!(PROCESSOR_RELATIONSHIP, GroupMask);
        let count = base + offset_of!(PROCESSOR_RELATIONSHIP, GroupCount);
        let mut data = Vec::new();
        for values in [&[1usize, 4][..], &[12]] {
            let size = masks + values.len() * size_of::<GROUP_AFFINITY>();
            let mut record = vec![0; size];
            record[4..8].copy_from_slice(&(size as u32).to_ne_bytes());
            record[count..count + 2].copy_from_slice(&(values.len() as u16).to_ne_bytes());
            for (group, mask) in record[masks..]
                .as_chunks_mut::<{ size_of::<GROUP_AFFINITY>() }>()
                .0
                .iter_mut()
                .zip(values)
            {
                group[..size_of::<usize>()].copy_from_slice(&mask.to_ne_bytes());
            }
            data.extend(record);
        }
        assert_eq!(count_cores(&data), Some((2, 4)));
        assert_eq!(count_cores(&data[..data.len() - 1]), None);
        assert_eq!(count_cores(&[0; 8]), None);
    }
}
