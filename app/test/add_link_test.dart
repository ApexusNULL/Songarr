import 'package:flutter_test/flutter_test.dart';
import 'package:songarr_app/ui/add_link.dart';

void main() {
  test('the link in shared or pasted text', () {
    expect(linkIn('https://youtu.be/abcDEF12345'), 'https://youtu.be/abcDEF12345');
    expect(linkIn('Check this out! https://www.youtube.com/watch?v=abcDEF12345&si=x shared via YouTube'),
        'https://www.youtube.com/watch?v=abcDEF12345&si=x');
    expect(linkIn('night drive nova band'), isNull); // a search, not a link
    expect(linkIn('youtube.com/watch?v=abc'), isNull); // (needs http(s)://)
  });
}
