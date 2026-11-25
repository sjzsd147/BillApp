export const MAX_STATEMENT_UPLOAD_BYTES = 50 * 1024 * 1024; // 50 MB
export const MAX_BACKUP_UPLOAD_BYTES = 200 * 1024 * 1024; // 200 MB

export type UploadSource = 'drop' | 'select' | 'upload';
export type FileSnapshotErrorCode =
  | 'FILE_DIRECTORY'
  | 'FILE_EMPTY'
  | 'FILE_TOO_LARGE'
  | 'FILE_UNREADABLE';

export interface CapturedFileTask {
  name: string;
  size: number;
  ext: string;
  isDirectory: boolean;
  source: UploadSource;
  snapshotPromise: Promise<File>;
}

const MEMORY_BACKED_FILES = new WeakSet<File>();

export function getFileExtension(fileName?: string): string {
  const raw = String(fileName || '').trim();
  const idx = raw.lastIndexOf('.');
  if (idx <= 0 || idx === raw.length - 1) {
    return '(none)';
  }
  const ext = raw.slice(idx).toLowerCase();
  return /^\.[a-z0-9]{1,12}$/.test(ext) ? ext : '(other)';
}

export class FileSnapshotError extends Error {
  code: FileSnapshotErrorCode;
  userMessage: string;
  causeName?: string;

  constructor(code: FileSnapshotErrorCode, userMessage: string, causeName?: string) {
    super(userMessage);
    this.name = 'FileSnapshotError';
    this.code = code;
    this.userMessage = userMessage;
    this.causeName = causeName;
  }
}

export function isMemoryBackedFile(file: File): boolean {
  return MEMORY_BACKED_FILES.has(file);
}

function readFileAsArrayBuffer(file: File): Promise<ArrayBuffer> {
  if (typeof file.arrayBuffer === 'function') {
    return file.arrayBuffer();
  }
  return new Promise<ArrayBuffer>((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      if (reader.result instanceof ArrayBuffer) {
        resolve(reader.result);
      } else {
        reject(new Error('FileReader did not produce an ArrayBuffer'));
      }
    };
    reader.onerror = () => {
      reject(reader.error || new Error('FileReader error'));
    };
    reader.readAsArrayBuffer(file);
  });
}

function buildUnreadableMessage(source: UploadSource): string {
  if (source === 'drop') {
    return '无法直接读取拖入的文件（常见于从邮件客户端如 Thunderbird、压缩包预览窗口或云盘直接拖入），请先保存到本地文件夹后再拖入或点击选择';
  }
  return '无法读取所选文件（可能已被移动、删除或正被 Excel/WPS 等程序独占占用），请关闭占用程序后重新选择';
}

function buildEmptyMessage(source: UploadSource): string {
  if (source === 'drop') {
    return '拖入的文件内容为空（0 字节，常见于从邮件客户端如 Thunderbird 或压缩包预览直接拖入虚拟附件），请先保存到本地文件夹后再导入';
  }
  return '所选文件内容为空（0 字节），请确认账单文件是否完整';
}

/**
 * Materialize a disk-backed or drag-and-drop File handle into an in-memory File.
 *
 * Detaching from the underlying disk/OLE path prevents Chromium/Electron
 * `net::ERR_UPLOAD_FILE_CHANGED`, `net::ERR_FILE_NOT_FOUND`, and `net::ERR_ACCESS_DENIED`
 * errors when temporary attachments (e.g. Thunderbird drags) are cleaned up or locked.
 */
export async function snapshotFileToMemory(
  file: File,
  options: { maxBytes?: number; source?: UploadSource } = {}
): Promise<File> {
  const maxBytes = options.maxBytes ?? MAX_STATEMENT_UPLOAD_BYTES;
  const source: UploadSource = options.source ?? 'upload';

  if (typeof file.size === 'number' && file.size > maxBytes) {
    const maxMb = Math.round(maxBytes / (1024 * 1024));
    throw new FileSnapshotError(
      'FILE_TOO_LARGE',
      `文件大小超过 ${maxMb} MB 安全限制，请拆分或解压后再导入`
    );
  }

  if (MEMORY_BACKED_FILES.has(file)) {
    return file;
  }

  let buffer: ArrayBuffer;
  try {
    buffer = await readFileAsArrayBuffer(file);
  } catch (err: any) {
    const causeName = err?.name ? String(err.name) : undefined;
    throw new FileSnapshotError('FILE_UNREADABLE', buildUnreadableMessage(source), causeName);
  }

  if (buffer.byteLength === 0) {
    throw new FileSnapshotError('FILE_EMPTY', buildEmptyMessage(source));
  }

  if (buffer.byteLength > maxBytes) {
    const maxMb = Math.round(maxBytes / (1024 * 1024));
    throw new FileSnapshotError(
      'FILE_TOO_LARGE',
      `文件大小超过 ${maxMb} MB 安全限制，请拆分或解压后再导入`
    );
  }

  const memFile = new File([buffer], file.name, {
    type: file.type,
    lastModified: Number.isFinite(file.lastModified) ? file.lastModified : Date.now(),
  });
  MEMORY_BACKED_FILES.add(memFile);
  return memFile;
}

/**
 * Synchronously inspect a DragEvent's DataTransfer and immediately start reading
 * dropped files into memory before the drop event yields and OS/mail-client temp
 * handles expire.
 */
export function captureDroppedFiles(
  dataTransfer: DataTransfer | null,
  maxBytes: number = MAX_STATEMENT_UPLOAD_BYTES
): { tasks: CapturedFileTask[]; transferTypes: string[] } {
  const transferTypes = dataTransfer?.types ? Array.from(dataTransfer.types) : [];
  const tasks: CapturedFileTask[] = [];
  if (!dataTransfer) {
    return { tasks, transferTypes };
  }

  const items = dataTransfer.items ? Array.from(dataTransfer.items) : [];
  const fileItems = items.filter((item) => item.kind === 'file');

  if (fileItems.length > 0) {
    for (const item of fileItems) {
      let isDirectory = false;
      let entryName = '';
      try {
        const entry =
          typeof (item as any).webkitGetAsEntry === 'function'
            ? (item as any).webkitGetAsEntry()
            : null;
        if (entry) {
          isDirectory = Boolean(entry.isDirectory);
          entryName = String(entry.name || '');
        }
      } catch {
        // Ignore unavailable webkitGetAsEntry implementations
      }

      const rawFile = typeof item.getAsFile === 'function' ? item.getAsFile() : null;
      const name = rawFile?.name || entryName || '未命名文件';
      const size = rawFile?.size ?? 0;
      const ext = getFileExtension(name);

      if (isDirectory) {
        const err = new FileSnapshotError(
          'FILE_DIRECTORY',
          '不支持直接拖入文件夹，请打开文件夹后选择或拖入其中的账单文件'
        );
        const rejected = Promise.reject(err);
        rejected.catch(() => {});
        tasks.push({ name, size, ext, isDirectory: true, source: 'drop', snapshotPromise: rejected });
        continue;
      }

      if (!rawFile) {
        const err = new FileSnapshotError('FILE_UNREADABLE', buildUnreadableMessage('drop'));
        const rejected = Promise.reject(err);
        rejected.catch(() => {});
        tasks.push({ name, size, ext, isDirectory: false, source: 'drop', snapshotPromise: rejected });
        continue;
      }

      const promise = snapshotFileToMemory(rawFile, { maxBytes, source: 'drop' });
      promise.catch(() => {});
      tasks.push({ name, size, ext, isDirectory: false, source: 'drop', snapshotPromise: promise });
    }
    return { tasks, transferTypes };
  }

  const rawFiles = dataTransfer.files ? Array.from(dataTransfer.files) : [];
  for (const rawFile of rawFiles) {
    const promise = snapshotFileToMemory(rawFile, { maxBytes, source: 'drop' });
    promise.catch(() => {});
    tasks.push({
      name: rawFile.name || '未命名文件',
      size: rawFile.size ?? 0,
      ext: getFileExtension(rawFile.name),
      isDirectory: false,
      source: 'drop',
      snapshotPromise: promise,
    });
  }

  return { tasks, transferTypes };
}

/**
 * Synchronously start reading files picked from an `<input type="file">` into memory
 * before resetting the input element value.
 */
export function captureSelectedFiles(
  fileList: FileList | File[],
  maxBytes: number = MAX_STATEMENT_UPLOAD_BYTES
): CapturedFileTask[] {
  const files = Array.from(fileList || []);
  return files.map((rawFile) => {
    const promise = snapshotFileToMemory(rawFile, { maxBytes, source: 'select' });
    promise.catch(() => {});
    return {
      name: rawFile.name || '未命名文件',
      size: rawFile.size ?? 0,
      ext: getFileExtension(rawFile.name),
      isDirectory: false,
      source: 'select' as const,
      snapshotPromise: promise,
    };
  });
}
