import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'session.dart';

/// Recent searches, newest first, kept on this phone for each profile. A search is remembered
/// when you submit it or tap one of its results, not for every letter typed.
class SearchHistory extends Notifier<List<String>> {
  static const max = 15;

  String? get _key {
    final s = ref.read(sessionProvider).value;
    return s == null ? null : 'search_history_${s.userId}';
  }

  @override
  List<String> build() {
    ref.watch(sessionProvider); // another profile signs in: its own history
    _load();
    return const [];
  }

  Future<void> _load() async {
    final key = _key;
    if (key == null) return;
    try {
      state = (await SharedPreferences.getInstance()).getStringList(key) ?? const [];
    } catch (_) {
      // storage unavailable: start empty
    }
  }

  Future<void> _save() async {
    final key = _key;
    if (key == null) return;
    try {
      await (await SharedPreferences.getInstance()).setStringList(key, state);
    } catch (_) {}
  }

  /// Remember [query] (moving it to the top if it was already there).
  void add(String query) {
    final q = query.trim();
    if (q.length < 2) return;
    final lower = q.toLowerCase();
    if (state.isNotEmpty && state.first.toLowerCase() == lower) return;
    state = [q, ...state.where((s) => s.toLowerCase() != lower)].take(max).toList();
    _save();
  }

  void remove(String query) {
    state = [for (final s in state) if (s != query) s];
    _save();
  }

  void clear() {
    state = const [];
    _save();
  }
}

final searchHistoryProvider = NotifierProvider<SearchHistory, List<String>>(SearchHistory.new);
