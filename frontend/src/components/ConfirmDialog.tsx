import { useEffect, useId, useRef } from 'react';
import type { ReactNode } from 'react';

export default function ConfirmDialog({
  title,
  children,
  onClose,
  initialFocus,
}: {
  title: string;
  children: ReactNode;
  onClose: () => void;
  initialFocus?: string;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const heading = useId();
  useEffect(() => {
    const element = dialog.current!;
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    element.showModal();
    if (initialFocus) element.querySelector<HTMLElement>(initialFocus)?.focus();
    return () => {
      element.close();
      document.body.style.overflow = previousOverflow;
      if (previous?.isConnected) previous.focus();
    };
  }, [initialFocus]);
  return (
    <dialog
      ref={dialog}
      className="confirm-dialog"
      aria-labelledby={heading}
      onCancel={(event) => {
        event.preventDefault();
        onClose();
      }}
    >
      <h2 id={heading}>{title}</h2>
      {children}
    </dialog>
  );
}
