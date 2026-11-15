import { onMounted, onUnmounted } from 'vue';
import type { Router } from 'vue-router';

export interface ShortcutHandlers {
  onSearch?: () => void;
  onHelp?: () => void;
  onEscape?: () => void;
  onRefresh?: () => void;
  onUndo?: () => void;
}

export function setupGlobalShortcuts(router: Router, handlers: ShortcutHandlers = {}) {
  function handleKeyDown(e: KeyboardEvent) {
    const target = e.target as HTMLElement | null;
    const isInput = target && (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA' || target.isContentEditable);

    // Escape works even in inputs
    if (e.key === 'Escape') {
      if (handlers.onEscape) {
        handlers.onEscape();
        return;
      }
    }

    // If typing in input, don't trigger navigation or letter shortcuts
    if (isInput) return;

    // Help shortcut: '?'
    if (e.key === '?' || e.key === 'F1') {
      e.preventDefault();
      handlers.onHelp?.();
      return;
    }

    // Ctrl / Meta key combinations
    if (e.ctrlKey || e.metaKey) {
      if (e.key.toLowerCase() === 'k') {
        e.preventDefault();
        handlers.onSearch?.();
        return;
      }
      if (e.key.toLowerCase() === 'i') {
        e.preventDefault();
        router.push('/import');
        return;
      }
      if (e.key === ',') {
        e.preventDefault();
        router.push('/settings');
        return;
      }
      if (e.key.toLowerCase() === 'z') {
        e.preventDefault();
        handlers.onUndo?.();
        return;
      }

      // Page jumps Ctrl+1..7
      switch (e.key) {
        case '1':
          e.preventDefault();
          router.push('/dashboard');
          break;
        case '2':
          e.preventDefault();
          router.push('/import');
          break;
        case '3':
          e.preventDefault();
          router.push('/review');
          break;
        case '4':
          e.preventDefault();
          router.push('/transactions');
          break;
        case '5':
          e.preventDefault();
          router.push('/rules');
          break;
        case '6':
          e.preventDefault();
          router.push('/accounts');
          break;
        case '7':
          e.preventDefault();
          router.push('/settings');
          break;
      }
    }
  }

  onMounted(() => {
    window.addEventListener('keydown', handleKeyDown);
  });

  onUnmounted(() => {
    window.removeEventListener('keydown', handleKeyDown);
  });
}
