import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import '../../i18n';
import { Modal } from '../ui';

describe('Modal closeOnBackdrop (port modal)', () => {
  const setup = (closeOnBackdrop: boolean) => {
    const onClose = vi.fn();
    render(
      <Modal open title="Port 7" onClose={onClose} closeOnBackdrop={closeOnBackdrop} testId="m">
        <p>body</p>
      </Modal>,
    );
    return onClose;
  };
  it('closes on a backdrop click, Escape and X', () => {
    const onClose = setup(true);
    const backdrop = screen.getByTestId('m-backdrop');
    fireEvent.mouseDown(backdrop);
    fireEvent.click(backdrop);
    expect(onClose).toHaveBeenCalledTimes(1);
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(onClose).toHaveBeenCalledTimes(2);
    fireEvent.click(screen.getByRole('button', { name: 'Close' }));
    expect(onClose).toHaveBeenCalledTimes(3);
  });
  it('a click inside the dialog or a drag that ends outside does not close it', () => {
    const onClose = setup(true);
    fireEvent.mouseDown(screen.getByText('body'));
    fireEvent.click(screen.getByTestId('m-backdrop'));
    fireEvent.click(screen.getByText('body'));
    expect(onClose).not.toHaveBeenCalled();
  });
  it('form dialogs keep the old behaviour (no backdrop close)', () => {
    const onClose = setup(false);
    const backdrop = screen.getByTestId('m-backdrop');
    fireEvent.mouseDown(backdrop);
    fireEvent.click(backdrop);
    expect(onClose).not.toHaveBeenCalled();
  });
  it('is rendered into a portal on <body>', () => {
    setup(true);
    expect(screen.getByRole('dialog').closest('body')).toBe(document.body);
    expect(screen.getByTestId('m-backdrop').parentElement).toBe(document.body);
  });
});
