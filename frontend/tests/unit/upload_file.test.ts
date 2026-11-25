import { afterEach, describe, expect, it, vi } from 'vitest';
import apiClient from '@/api/client';
import { previewImport } from '@/api/import';
import {
  FileSnapshotError,
  captureDroppedFiles,
  captureSelectedFiles,
  getFileExtension,
  isMemoryBackedFile,
  snapshotFileToMemory,
} from '@/utils/uploadFile';

describe('uploadFile snapshotting and drag-drop hardening', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('materializes disk-backed files into memory and avoids redundant re-copy', async () => {
    const diskFile = new File(['hello,world\n1,2\n'], 'alipay.csv', { type: 'text/csv' });
    expect(isMemoryBackedFile(diskFile)).toBe(false);

    const memFile = await snapshotFileToMemory(diskFile, { source: 'drop' });
    expect(memFile).not.toBe(diskFile);
    expect(isMemoryBackedFile(memFile)).toBe(true);
    expect(memFile.name).toBe('alipay.csv');
    expect(memFile.size).toBe(diskFile.size);

    const second = await snapshotFileToMemory(memFile, { source: 'upload' });
    expect(second).toBe(memFile);

    // Tighter maxBytes must still be enforced even for already memory-backed files
    await expect(snapshotFileToMemory(memFile, { maxBytes: 4, source: 'upload' })).rejects.toMatchObject({
      code: 'FILE_TOO_LARGE',
    });
  });

  it('maps unreadable virtual/Thunderbird drag handles to a clear FileSnapshotError', async () => {
    const brokenFile = new File(['stub'], 'wechat.zip', { type: 'application/zip' });
    Object.defineProperty(brokenFile, 'arrayBuffer', {
      value: () => Promise.reject(Object.assign(new Error('Could not read file'), { name: 'NotReadableError' })),
    });

    await expect(snapshotFileToMemory(brokenFile, { source: 'drop' })).rejects.toMatchObject({
      name: 'FileSnapshotError',
      code: 'FILE_UNREADABLE',
      causeName: 'NotReadableError',
    });

    try {
      await snapshotFileToMemory(brokenFile, { source: 'drop' });
    } catch (err: any) {
      expect(err).toBeInstanceOf(FileSnapshotError);
      expect(err.userMessage).toContain('Thunderbird');
    }
  });

  it('rejects 0-byte virtual stubs and oversized files before upload', async () => {
    const emptyFile = new File([], 'empty.pdf', { type: 'application/pdf' });
    await expect(snapshotFileToMemory(emptyFile, { source: 'drop' })).rejects.toMatchObject({
      code: 'FILE_EMPTY',
    });

    const tinyMaxFile = new File(['123456'], 'big.pdf', { type: 'application/pdf' });
    await expect(snapshotFileToMemory(tinyMaxFile, { maxBytes: 4, source: 'select' })).rejects.toMatchObject({
      code: 'FILE_TOO_LARGE',
    });
  });

  it('detects dropped directories, empty virtual transfers, and readable files synchronously', async () => {
    // 1. Empty DataTransfer (e.g. Thunderbird drag exposing only moz-url without file handle)
    const emptyDt = {
      types: ['text/x-moz-url', 'application/x-moz-file-promise-url'],
      items: [],
      files: [],
    } as unknown as DataTransfer;
    const emptyRes = captureDroppedFiles(emptyDt);
    expect(emptyRes.tasks).toEqual([]);
    expect(emptyRes.transferTypes).toContain('text/x-moz-url');

    // 2. Directory + valid file in DataTransfer.items
    const validFile = new File(['%PDF-1.4\nbody'], 'bank.pdf', { type: 'application/pdf' });
    const dirFile = new File([], 'bills_folder');
    const dtWithItems = {
      types: ['Files'],
      items: [
        {
          kind: 'file',
          getAsFile: () => dirFile,
          webkitGetAsEntry: () => ({ isDirectory: true, name: 'bills_folder' }),
        },
        {
          kind: 'file',
          getAsFile: () => validFile,
          webkitGetAsEntry: () => ({ isDirectory: false, name: 'bank.pdf' }),
        },
      ],
      files: [dirFile, validFile],
    } as unknown as DataTransfer;

    const captured = captureDroppedFiles(dtWithItems);
    expect(captured.tasks.length).toBe(2);
    expect(captured.tasks[0].isDirectory).toBe(true);
    await expect(captured.tasks[0].snapshotPromise).rejects.toMatchObject({ code: 'FILE_DIRECTORY' });

    const snap = await captured.tasks[1].snapshotPromise;
    expect(isMemoryBackedFile(snap)).toBe(true);
    expect(snap.name).toBe('bank.pdf');
  });

  it('captures selected files and sanitizes extensions without exposing filenames', async () => {
    expect(getFileExtension('张三_招商银行.PDF')).toBe('.pdf');
    expect(getFileExtension('noext')).toBe('(none)');

    const f = new File(['csv,data'], 'bill.csv', { type: 'text/csv' });
    const tasks = captureSelectedFiles([f]);
    expect(tasks.length).toBe(1);
    expect(tasks[0].ext).toBe('.csv');
    const mem = await tasks[0].snapshotPromise;
    expect(isMemoryBackedFile(mem)).toBe(true);
  });

  it('previewImport materializes file into memory and omits manual multipart Content-Type header', async () => {
    const postSpy = vi.spyOn(apiClient, 'post').mockResolvedValue({
      token: 't1',
      file_name: 'bill.zip',
      source: 'wechat',
      total_rows: 1,
      need_password: false,
      need_mapping: false,
      accounts: [],
      self_checks: [],
      rows: [],
      warnings: [],
      expires_at: '',
    } as any);

    const diskFile = new File(['PK\x03\x04content'], 'bill.zip', { type: 'application/zip' });
    await previewImport(diskFile);

    expect(postSpy).toHaveBeenCalledTimes(1);
    const [url, body, config] = postSpy.mock.calls[0];
    expect(url).toBe('/import/preview');
    expect(body).toBeInstanceOf(FormData);
    const uploaded = (body as FormData).get('file');
    expect(uploaded).toBeInstanceOf(File);
    expect(isMemoryBackedFile(uploaded as File)).toBe(true);
    expect((config as any)?.headers).toBeUndefined();
  });
});
