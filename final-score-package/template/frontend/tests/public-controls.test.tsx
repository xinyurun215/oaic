import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import * as Dialog from '@radix-ui/react-dialog';
import * as Menu from '@radix-ui/react-dropdown-menu';

test('dialog closes with Escape and restores focus', async () => {
  const user = userEvent.setup();
  render(<Dialog.Root>
    <Dialog.Trigger>Open dialog</Dialog.Trigger>
    <Dialog.Portal><Dialog.Overlay /><Dialog.Content>
      <Dialog.Title>Example dialog</Dialog.Title>
      <Dialog.Description>Generic control example.</Dialog.Description>
      <Dialog.Close>Close</Dialog.Close>
    </Dialog.Content></Dialog.Portal>
  </Dialog.Root>);
  const trigger = screen.getByRole('button', { name: 'Open dialog' });
  await user.click(trigger);
  expect(await screen.findByRole('dialog', { name: 'Example dialog' })).toBeVisible();
  await user.keyboard('{Escape}');
  await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  await waitFor(() => expect(trigger).toHaveFocus());
});

test('menu supports keyboard selection', async () => {
  const user = userEvent.setup();
  const selected = vi.fn();
  render(<Menu.Root>
    <Menu.Trigger>Open menu</Menu.Trigger>
    <Menu.Portal><Menu.Content>
      <Menu.Item onSelect={selected}>Example action</Menu.Item>
    </Menu.Content></Menu.Portal>
  </Menu.Root>);
  screen.getByRole('button', { name: 'Open menu' }).focus();
  await user.keyboard('{ArrowDown}');
  const item = await screen.findByRole('menuitem', { name: 'Example action' });
  await waitFor(() => expect(item).toHaveFocus());
  await user.keyboard('{Enter}');
  expect(selected).toHaveBeenCalledTimes(1);
  await waitFor(() => expect(screen.queryByRole('menu')).not.toBeInTheDocument());
});
