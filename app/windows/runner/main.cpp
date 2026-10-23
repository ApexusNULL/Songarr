#include <flutter/dart_project.h>
#include <flutter/flutter_view_controller.h>
#include <flutter_windows.h>
#include <windows.h>

#include <algorithm>

#include "flutter_window.h"
#include "utils.h"

int APIENTRY wWinMain(_In_ HINSTANCE instance, _In_opt_ HINSTANCE prev,
                      _In_ wchar_t *command_line, _In_ int show_command) {
  // One Songarr at a time (two would play over each other): a second start brings the first forward.
  HANDLE single = ::CreateMutexW(nullptr, TRUE, L"Local\\SongarrDesktop");
  if (single != nullptr && ::GetLastError() == ERROR_ALREADY_EXISTS) {
    if (HWND other = ::FindWindowW(L"SONGARR_DESKTOP_WINDOW", nullptr)) {
      ::ShowWindow(other, ::IsIconic(other) ? SW_RESTORE : SW_SHOW);
      ::SetForegroundWindow(other);
    }
    return EXIT_SUCCESS;
  }

  // Attach to console when present (e.g., 'flutter run') or create a
  // new console when running with a debugger.
  if (!::AttachConsole(ATTACH_PARENT_PROCESS) && ::IsDebuggerPresent()) {
    CreateAndAttachConsole();
  }

  // Initialize COM, so that it is available for use in the library and/or
  // plugins.
  ::CoInitializeEx(nullptr, COINIT_APARTMENTTHREADED);

  flutter::DartProject project(L"data");

  std::vector<std::string> command_line_arguments =
      GetCommandLineArguments();

  project.set_dart_entrypoint_arguments(std::move(command_line_arguments));

  // 1280 x 820 (smaller on a small screen), in the middle of the screen's work area. The window
  // scales these by the screen's DPI, so they're worked out unscaled here.
  FlutterWindow window(project);
  RECT work{0, 0, 1280, 820};
  ::SystemParametersInfoW(SPI_GETWORKAREA, 0, &work, 0);
  HMONITOR monitor = ::MonitorFromPoint({work.left, work.top}, MONITOR_DEFAULTTOPRIMARY);
  double scale = FlutterDesktopGetDpiForMonitor(monitor) / 96.0;
  double work_width = (work.right - work.left) / scale, work_height = (work.bottom - work.top) / scale;
  unsigned int width = static_cast<unsigned int>(std::min(1280.0, work_width - 40));
  unsigned int height = static_cast<unsigned int>(std::min(820.0, work_height - 40));
  Win32Window::Point origin(static_cast<unsigned int>(work.left / scale + (work_width - width) / 2),
                            static_cast<unsigned int>(work.top / scale + (work_height - height) / 2));
  Win32Window::Size size(width, height);
  if (!window.Create(L"Songarr", origin, size)) {
    return EXIT_FAILURE;
  }
  window.SetQuitOnClose(true);

  ::MSG msg;
  while (::GetMessage(&msg, nullptr, 0, 0)) {
    ::TranslateMessage(&msg);
    ::DispatchMessage(&msg);
  }

  ::CoUninitialize();
  return EXIT_SUCCESS;
}
