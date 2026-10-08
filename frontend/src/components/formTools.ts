export function errorText(error: unknown): string {
  return error instanceof Error ? error.message : 'Не удалось выполнить действие. Проверьте соединение и повторите.';
}

export function errorStatus(error: unknown): number {
  return typeof error === 'object' && error !== null && 'status' in error ? Number((error as { status: unknown }).status) : 0;
}

export function readDraft<T>(key: string, fallback: T): T {
  try { const value = localStorage.getItem(key); return value ? { ...fallback, ...JSON.parse(value) } : fallback; }
  catch { return fallback; }
}

export function storeDraft(key: string, value: unknown): void {
  try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* The open form still preserves its draft. */ }
}

export function clearDraft(key: string): void {
  try { localStorage.removeItem(key); } catch { /* Storage may be unavailable. */ }
}

export function operationId(): string {
  if (typeof crypto.randomUUID === 'function') return crypto.randomUUID();
  // getRandomValues is available on ordinary LAN pages as well as HTTPS.
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, value => value.toString(16).padStart(2, '0')).join('');
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

// Forms explicitly show Kostanay time; the browser's own time zone is irrelevant.
export function dateInput(date = new Date()): string {
  return new Date(date.getTime() + 5 * 60 * 60 * 1000).toISOString().slice(0, 16);
}

export function inputToIso(value: string): string {
  if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(value)) throw new Error('Выберите дату и время.');
  const date = new Date(value + ':00+05:00');
  if (!Number.isFinite(date.getTime())) throw new Error('Дата и время указаны неверно.');
  return date.toISOString();
}

export async function compressImage(file: File): Promise<File> {
  if (!['image/jpeg', 'image/png', 'image/webp'].includes(file.type)) throw new Error('Выберите фото JPEG, PNG или WebP. Если телефон снимает HEIC, выберите совместимый формат.');
  if (file.size > 30 * 1024 * 1024) throw new Error('Фото слишком большое. Выберите изображение до 30 МБ.');
  const url = URL.createObjectURL(file);
  try {
    const image = new Image();
    image.src = url;
    try { await image.decode(); } catch { throw new Error('Не удалось открыть фото. Выберите другое изображение JPEG, PNG или WebP.'); }
    const scale = Math.min(1, 1600 / Math.max(image.naturalWidth, image.naturalHeight));
    const canvas = document.createElement('canvas');
    canvas.width = Math.max(1, Math.round(image.naturalWidth * scale));
    canvas.height = Math.max(1, Math.round(image.naturalHeight * scale));
    const context = canvas.getContext('2d');
    if (!context) throw new Error('Камера недоступна в этом браузере. Попробуйте выбрать другое фото.');
    context.fillStyle = '#fff';
    context.fillRect(0, 0, canvas.width, canvas.height);
    context.drawImage(image, 0, 0, canvas.width, canvas.height);
    const blob = await new Promise<Blob>((resolve, reject) => canvas.toBlob(value => value ? resolve(value) : reject(new Error('Не удалось подготовить фото.')), 'image/jpeg', 0.82));
    return new File([blob], file.name.replace(/\.[^.]+$/, '') + '.jpg', { type: 'image/jpeg', lastModified: file.lastModified });
  } finally { URL.revokeObjectURL(url); }
}
