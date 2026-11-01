package com.example.menu1;

import android.os.Bundle;
import android.widget.Toast;
import androidx.annotation.NonNull;
import androidx.appcompat.app.AppCompatActivity;
import android.view.Menu;
import android.view.MenuItem;

public class MainActivity extends AppCompatActivity {

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);
    }

    @Override
    public boolean onCreateOptionsMenu(Menu menu) {
        getMenuInflater().inflate(R.menu.menu_main, menu);
        return true;
    }

    @Override
    public boolean onOptionsItemSelected(@NonNull MenuItem item) {

        int id = item.getItemId();

        if (id == R.id.action_settings) {
            Toast.makeText(MainActivity.this,
                    "Settings Clicked", Toast.LENGTH_LONG).show();
            return true;
        }
        else if (id == R.id.action_about) {
            Toast.makeText(MainActivity.this,
                    "About Clicked", Toast.LENGTH_LONG).show();
            return true;
        }
        else if (id == R.id.action_logout) {
            Toast.makeText(MainActivity.this,
                    "Logout Clicked", Toast.LENGTH_LONG).show();
            return true;
        }
        else {
            return super.onOptionsItemSelected(item);
        }
    }
}