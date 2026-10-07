import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:songarr_app/state/search_history.dart';
import 'package:songarr_app/state/session.dart';

class _SignedIn extends SessionNotifier {
  _SignedIn(this.userId);
  final int userId;
  @override
  Future<Session?> build() async => Session(server: 'https://music.example.com', token: 't', userId: userId, userName: 'Alex');
}

Future<ProviderContainer> _signedIn(int userId) async {
  final c = ProviderContainer(overrides: [sessionProvider.overrideWith(() => _SignedIn(userId))]);
  await c.read(sessionProvider.future);
  c.read(searchHistoryProvider);
  await Future<void>.delayed(Duration.zero); // let it load
  return c;
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  test('recent searches: newest first, no repeats, kept for each profile', () async {
    SharedPreferences.setMockInitialValues({});
    final c = await _signedIn(1);
    final h = c.read(searchHistoryProvider.notifier);
    h.add('interstellar');
    h.add('hans zimmer');
    h.add('Interstellar'); // again, any case: moves to the top, not added twice
    h.add(' x '); // too short to be worth keeping
    expect(c.read(searchHistoryProvider), ['Interstellar', 'hans zimmer']);
    for (var i = 0; i < 20; i++) {
      h.add('search $i');
    }
    expect(c.read(searchHistoryProvider).length, SearchHistory.max);
    expect(c.read(searchHistoryProvider).first, 'search 19');
    h.remove('search 19');
    expect(c.read(searchHistoryProvider).first, 'search 18');
    await Future<void>.delayed(Duration.zero);

    // it's still there when the app opens again
    final again = await _signedIn(1);
    expect(again.read(searchHistoryProvider).first, 'search 18');
    // another profile has its own
    final other = await _signedIn(2);
    expect(other.read(searchHistoryProvider), isEmpty);

    h.clear();
    expect(c.read(searchHistoryProvider), isEmpty);
    for (final x in [c, again, other]) {
      x.dispose();
    }
  });
}
