import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:songarr_app/api/client.dart';
import 'package:songarr_app/api/models.dart';
import 'package:songarr_app/state/data.dart';
import 'package:songarr_app/state/session.dart';
import 'package:songarr_app/ui/discography.dart';
import 'package:songarr_app/ui/theme.dart';

class _Api extends SongarrApi {
  _Api() : super(server: 'http://test', token: 't');
  List<String>? added;

  @override
  Future<void> addDiscography(int deezerId, String name, List<String> ids) async => added = ids;
}

Map<String, dynamic> _release(int id, String name, String type, String year) =>
    {'source': 'deezer', 'id': id, 'name': name, 'artists': ['Nova Band'], 'type': type, 'year': year, 'on_server': 0};

void main() {
  final discography = Discography({
    'releases': [
      _release(6, 'New Song', 'single', '2012'),
      _release(5, 'Greatest Hits', 'album', '2010'), // (Deezer calls hits albums albums)
      _release(4, 'Live at Home', 'album', '2008'),
      _release(3, 'Odds and Ends', 'ep', '2006'),
      _release(2, 'Second Wind', 'album', '2005'),
      _release(1, 'First Light', 'album', '2001'),
    ],
    'adding': null,
  });

  testWidgets('the discography, and adding it: albums and EPs to start with', (tester) async {
    final api = _Api();
    tester.view.physicalSize = const Size(1080, 2340); // a phone, so the whole sheet fits
    tester.view.devicePixelRatio = 2.625;
    addTearDown(tester.view.reset);
    await tester.pumpWidget(ProviderScope(
      overrides: [
        apiProvider.overrideWithValue(api),
        discographyProvider.overrideWith((ref, key) async => discography),
      ],
      child: MaterialApp(
        theme: songarrTheme(),
        home: const Scaffold(body: SingleChildScrollView(child: DiscographySection(deezerId: 412, name: 'Nova Band'))),
      ),
    ));
    await tester.pumpAndSettle();
    expect(find.text('Discography'), findsOneWidget);
    expect(find.text('4 albums · 1 EP · 1 single'), findsOneWidget);
    expect(find.text('Greatest Hits'), findsOneWidget); // albums first, newest first
    expect(find.text('New Song'), findsNothing);
    await tester.tap(find.text('Singles and EPs'));
    await tester.pumpAndSettle();
    expect(find.text('New Song'), findsOneWidget);
    expect(find.text('Greatest Hits'), findsNothing);

    await tester.tap(find.text('Add all'));
    await tester.pumpAndSettle();
    expect(find.text('Add Nova Band\'s discography'), findsOneWidget);
    expect(find.text('Albums (3)'), findsOneWidget);
    expect(find.text('Live albums (1)'), findsOneWidget); // a live album is an album to Deezer
    expect(find.text('Add 4 releases'), findsOneWidget); // three albums and the EP; not the live one or the single
    expect(tester.widget<CheckboxListTile>(find.widgetWithText(CheckboxListTile, 'Albums (3)')).value, isTrue);
    expect(tester.widget<CheckboxListTile>(find.widgetWithText(CheckboxListTile, 'Live at Home')).value, isFalse);
    await tester.tap(find.text('Live albums (1)')); // the whole group
    await tester.pumpAndSettle();
    expect(find.text('Add 5 releases'), findsOneWidget);
    await tester.tap(find.widgetWithText(CheckboxListTile, 'Greatest Hits')); // and one off again
    await tester.pumpAndSettle();
    await tester.tap(find.text('Add 4 releases'));
    await tester.pumpAndSettle();
    expect(api.added, ['4', '3', '2', '1']); // as shown, newest first (the server sorts them itself)
    expect(find.text('Add Nova Band\'s discography'), findsNothing);
    expect(find.textContaining('Adding 4 releases'), findsOneWidget);
  });
}
